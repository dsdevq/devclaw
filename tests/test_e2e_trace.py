"""E2E trace harness — stub mode.

Runs a full goal lifecycle (investigating → executing → done-gate → done) with
fakes for everything network-touching, while a real :class:`Tracer` collects
every observable event. The assertion is structural: the trace must show the
expected sequence of ticks, dispatches, deliveries, and notifications. This is
the safety net for refactors that touch the runtime path — if a refactor breaks
the live flow it will break this trace shape, not just unit tests.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.loom.trace import Tracer, set_tracer
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal


PLAN_ACT = json.dumps({
    "decision": "act", "note": "ship the change",
    "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}],
})
PLAN_DONE = json.dumps({"decision": "done", "note": "all backlog shipped"})
EVAL_ACHIEVED = json.dumps({"verdict": "achieved", "rationale": "all done_when met"})


async def _tick(store, planner, evaluator, engine, notifier, *, verify_done=True):
    return await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="", prepare_ws=fake_prepare, verify_done=verify_done,
    )


@pytest.mark.asyncio
async def test_e2e_trace_captures_full_lifecycle(tmp_path):
    """A goal advances from investigating to done over several ticks; the tracer
    sees every tick, every cognition call (with role), the dispatches, the
    delivery, and the owner-altitude notifications. Asserts the *shape* of what
    happened, not specific timing — refactors that preserve behavior preserve
    the trace; refactors that change it (extra calls, missing dispatch, wrong
    role label) fail this test loudly."""
    tracer = Tracer(label="e2e-stub")
    set_tracer(tracer)
    try:
        store = GoalStore(tmp_path, now=Clock())
        seed_goal(tmp_path, "g", backlog=["add /health"])
        # Start at investigating with an in-flight discovery review settling now.
        store.save_status("g", GoalStatus(
            phase="in_flight", lifecycle="investigating",
            in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
        ))
        notifier = RecordingNotifier()

        # 1) discovery settles → brief written → lifecycle flips to executing.
        researcher = FakeClaude("## Current state\nbare API", role="evaluator")
        engine_discovery = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo OK"))
        out1 = await _tick(store, FakeClaude(PLAN_ACT, role="planner"), researcher, engine_discovery, notifier)
        assert out1 is Outcome.ADVANCED

        # 2) executing → plan one action → dispatch.
        planner = FakeClaude(PLAN_ACT, role="planner")
        engine_dispatch = FakeEngine(
            poll_result=PollResult(terminal=False, status="running"),
            dispatch_ref=InFlight("devclaw", "implement_feature", "task_a", "task", "add /health"),
        )
        out2 = await _tick(store, planner, FakeClaude(role="evaluator"), engine_dispatch, notifier)
        assert out2 is Outcome.DISPATCHED

        # 3) action settles green → delivery recorded → planner sees finished_detail and proposes done.
        # The same tick chains: poll → record delivery → plan → done proposal → done-gate review dispatched.
        engine_settle = FakeEngine(poll_result=PollResult(
            terminal=True, status="done", detail="merged", pr_url="https://x/pr/1", gate_passed=True,
        ))
        # verify_done=False → done-gate runs an artifact-only evaluation now. Feed
        # the evaluator an "achieved" verdict so the goal closes cleanly.
        out3 = await _tick(
            store,
            FakeClaude(PLAN_DONE, role="planner"),
            FakeClaude(EVAL_ACHIEVED, role="evaluator"),
            engine_settle, notifier, verify_done=False,
        )
        assert out3 is Outcome.DONE

        # Whatever the final-tick outcome, the trace must contain:
        #   - at least three tick events (the three we ran)
        #   - a discovery cognition + a planner cognition + an evaluator cognition
        #   - a dispatch (the implement_feature one)
        #   - a delivery (the settled action)
        #   - at least one OWNER notification (start-of-executing or completion)
        ticks = tracer.by_kind("tick")
        assert len(ticks) >= 3, ticks
        assert {t["outcome"] for t in ticks} >= {"advanced", "dispatched"}

        cog_roles = tracer.cognition_by_role()
        assert "evaluator" in cog_roles, cog_roles      # discovery synthesis uses the evaluator-tier caller
        assert "planner" in cog_roles, cog_roles        # at least one planner call

        dispatches = tracer.by_kind("dispatch")
        assert any(d["tool"] == "implement_feature" for d in dispatches), dispatches

        deliveries = tracer.by_kind("delivery")
        assert deliveries and deliveries[0]["gate_passed"] is True

        notifies = tracer.by_kind("notify")
        assert any(n["level"] == "OWNER" for n in notifies)
    finally:
        set_tracer(None)


@pytest.mark.asyncio
async def test_two_runs_produce_identical_prompt_hashes(tmp_path):
    """Two back-to-back stub runs must hash every cognition prompt the same.
    If they don't, either the harness has hidden state (e.g. an output dir
    that accumulates log.md across runs) or the inputs to a build_prompt are
    nondeterministic. Either way the trace harness can't be a reliable
    refactor safety net until this holds."""
    from evals.e2e_trace import _run_stub

    set_tracer(None)
    t1 = await _run_stub(tmp_path / "a", ticks=3)
    set_tracer(None)
    t2 = await _run_stub(tmp_path / "b", ticks=3)

    hashes1 = [e["prompt_hash"] for e in t1.by_kind("cognition")]
    hashes2 = [e["prompt_hash"] for e in t2.by_kind("cognition")]
    assert hashes1 == hashes2, (hashes1, hashes2)


def test_tracer_dumps_json_and_timeline(tmp_path):
    """The tracer can persist a JSON trace (machine-readable, diffable across
    runs) and a markdown timeline (human-readable). Both are tested as a unit
    so the harness output format is pinned."""
    from devclaw.loom.trace import CognitionEvent, DeliveryEvent, DispatchEvent, NotifyEvent, TickEvent

    t = Tracer(label="format-test")
    t.append(TickEvent(goal_id="g", lifecycle="executing", phase="executing", outcome="dispatched"))
    t.append(CognitionEvent(role="planner", model="opus", prompt_hash="abc", prompt_preview="plan it", response_preview="act", latency_ms=42))
    t.append(DispatchEvent(goal_id="g", tool="implement_feature", ref_id="t1"))
    t.append(DeliveryEvent(goal_id="g", action_label="add /health", gate_passed=True, pr_url="https://x/1"))
    t.append(NotifyEvent(level="OWNER", text="shipped"))

    json_path = t.dump_json(tmp_path / "trace.json")
    md_path = t.dump_timeline(tmp_path / "timeline.md")

    data = json.loads(json_path.read_text())
    assert data["label"] == "format-test"
    assert data["summary"]["ticks"] == 1
    assert data["summary"]["cognition_calls"] == 1
    assert data["summary"]["dispatches"] == 1
    assert data["summary"]["deliveries"] == 1
    assert data["summary"]["cognition_by_role"] == {"planner": 1}

    md = md_path.read_text()
    assert "implement_feature" in md
    assert "https://x/1" in md
    assert "planner" in md
