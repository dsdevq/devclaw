"""The goal heartbeat — the load-bearing zero-token guardrail + the evaluation
state machine (in_flight → verifying → done). Folded from goalclaw, extended for
the direction evaluator + done-gate.

The single most important assertions in this layer: an idle tick and an
in-flight-still-running tick must leave BOTH cognition callers (planner +
evaluator) at calls == 0. If those ever go non-zero, the Pro quota dies under N
idle ticks/day.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal_models import GoalStatus, InFlight, PollResult
from devclaw.goal_store import GoalStore
from devclaw.goal_tick import Outcome, tick_goal
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

ACT = json.dumps(
    {"decision": "act", "note": "ship next", "actions": [{"tool": "start_program", "goal": "build /health"}]}
)
ACT_FEATURE = json.dumps(
    {"decision": "act", "note": "feat", "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}]}
)


def _store(tmp_path, clock):
    return GoalStore(tmp_path, now=clock)


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, eval_every=99, verify_done=True, summary_caller=None, merger=None, grill_caller=None):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare,
        eval_every=eval_every, verify_done=verify_done, summary_caller=summary_caller,
        merger=merger, grill_caller=grill_caller,
    )


class RecordingMerger:
    """A fake auto-merger: records the PR urls it was asked to merge."""

    def __init__(self, ok: bool = True):
        self.merged: list[str] = []
        self._ok = ok

    async def __call__(self, pr_url: str) -> bool:
        self.merged.append(pr_url)
        return self._ok


class RecordingSummarizer:
    """A fake plain-language summarizer caller: records prompts and returns a
    fixed plain rewrite, so tests can assert WHICH notifications get summarized."""

    def __init__(self, rewrite="PLAIN: here is what is happening"):
        self.prompts: list[str] = []
        self._rewrite = rewrite

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._rewrite


# ---- the guardrail ---------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_tick_spends_zero_tokens(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IDLE
    assert planner.calls == 0          # <-- the quota guardrail
    assert evaluator.calls == 0        # <-- evaluator must not fire on idle either
    assert engine.dispatched == []
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_in_flight_running_spends_zero_tokens(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "start_program", "p1", "program")),
    )
    planner, evaluator = FakeClaude(ACT), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(terminal=False, status="running"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IN_FLIGHT
    assert planner.calls == 0 and evaluator.calls == 0
    assert engine.polls == 1


# ---- the working path ------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tick_plans_and_dispatches(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")  # no STATUS yet → cadence due → plan
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 1
    assert evaluator.calls == 0
    assert len(engine.dispatched) == 1
    action, goal, notify_url = engine.dispatched[0]
    assert action.tool == "start_program"
    assert notify_url == "http://relay"
    saved = store.load_status("g")
    assert saved.phase == "in_flight" and saved.in_flight is not None


@pytest.mark.asyncio
async def test_workspace_prepped_before_dispatch(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    calls: list = []

    async def rec_prepare(ws, repo_url=None):
        calls.append((ws, repo_url))
        return "main"

    out = await tick_goal(
        "g", store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="", prepare_ws=rec_prepare, eval_every=99,
    )
    assert out is Outcome.DISPATCHED
    assert calls == [("/repos/demo", None)]
    assert len(engine.dispatched) == 1


@pytest.mark.asyncio
async def test_finished_action_records_delivery_and_replans(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health")),
    )
    planner = FakeClaude(ACT_FEATURE)
    evaluator = FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="Agent summary: added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 1
    assert "done" in planner.last_prompt           # finished result fed to planner
    # grounded delivery captured + PR logged
    assert "added /health" in store.recent_deliveries("g")
    assert "PR https://github.com/o/r/pull/9" in store.recent_log("g")


@pytest.mark.asyncio
async def test_steering_triggers_plan_even_when_cadence_not_due(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    (tmp_path / "g" / "inbox.md").write_text("pause features, fix the failing CI first\n")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 1
    assert "failing CI" in planner.last_prompt
    assert store.load_status("g").inbox_cursor == 1


# ---- blocked ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_goal_stays_idle_without_steering(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1h")
    store.save_status("g", GoalStatus(phase="blocked", blocked_on="which DB?", last_plan_at="2026-06-01T00:00:00+00:00"))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IDLE
    assert planner.calls == 0 and evaluator.calls == 0


@pytest.mark.asyncio
async def test_dispatch_cap_blocks_runaway(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")  # backlog 2 → cap = 4
    store.save_status("g", GoalStatus(phase="idle", actions_dispatched=4))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert engine.dispatched == []
    assert any("cap" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_planner_blocked_notifies(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert store.load_status("g").blocked_on == "which auth provider?"
    assert any("auth provider" in m for m in notifier.sent)


def test_steer_goal_resets_dispatch_counter_on_blocked(tmp_path):
    """steer_goal must zero actions_dispatched when unblocking so the dispatch
    cap doesn't re-fire on the very next tick after the human resolves the block."""
    from devclaw.goal_service import GoalConfig, GoalService
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    goals_dir = tmp_path / "goals"
    seed_goal(goals_dir, "g")
    goal_store = _store(goals_dir, Clock())
    goal_store.save_status("g", GoalStatus(phase="blocked", blocked_on="cap hit", actions_dispatched=5))

    db = StateStore(str(tmp_path / "state.db"))
    try:
        cfg = GoalConfig(goals_dir=goals_dir, notify_url="", tick_seconds=900, eval_every=99, verify_done=False)
        svc = GoalService(TaskQueue(db), db, config=cfg)

        svc.steer_goal("g", "resume with new approach")

        saved = goal_store.load_status("g")
        assert saved.phase == "idle"
        assert saved.actions_dispatched == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_done_goal_is_skipped(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="done"))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.SKIP_DONE
    assert planner.calls == 0 and evaluator.calls == 0


# ---- the done-gate (the planner's "done" is only a proposal) ---------------


@pytest.mark.asyncio
async def test_planner_done_opens_verification_review(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "done", "note": "all backlog merged"}))
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, verify_done=True)

    assert out is Outcome.VERIFYING
    assert evaluator.calls == 0            # eval runs when the review COMES BACK, not now
    assert len(engine.dispatched) == 1
    review_action, _, _ = engine.dispatched[0]
    assert review_action.tool == "review_repository"
    saved = store.load_status("g")
    assert saved.phase == "verifying"
    assert saved.in_flight is not None and saved.in_flight.is_done_check is True


@pytest.mark.asyncio
async def test_done_gate_review_achieved_closes_goal(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))
    planner = FakeClaude(ACT)  # must NOT be called
    evaluator = FakeClaude(json.dumps({"verdict": "achieved", "rationale": "/health exists and is tested"}))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo has /health + test"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DONE
    assert evaluator.calls == 1
    assert planner.calls == 0               # the done-gate doesn't re-plan
    assert "repo has /health" in evaluator.last_prompt   # review report fed in
    assert store.load_status("g").phase == "done"
    assert any("complete (verified)" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_done_gate_review_off_track_steers_and_continues(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))
    planner = FakeClaude(ACT)
    evaluator = FakeClaude(json.dumps({
        "verdict": "off_track", "rationale": "/health is not tested",
        "corrections": ["add a test for /health"],
    }))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="no test found"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.SLEPT             # not done
    s = store.load_status("g")
    assert s.phase == "idle"
    # the correction was steered back in for the next plan
    assert "add a test for /health" in store.unread_steering("g", GoalStatus(inbox_cursor=0))


@pytest.mark.asyncio
async def test_done_gate_disabled_uses_artifact_eval(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "done", "note": "done"}))
    evaluator = FakeClaude(json.dumps({"verdict": "achieved", "rationale": "deliveries show done_when met"}))
    engine, notifier = FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, verify_done=False)

    assert out is Outcome.DONE
    assert evaluator.calls == 1
    assert engine.dispatched == []          # no review run when verification disabled
    assert store.load_status("g").phase == "done"


# ---- periodic direction evaluation -----------------------------------------


@pytest.mark.asyncio
async def test_midflight_eval_fires_on_delivery_cadence_and_steers(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    # a feature just finished → deliveries_since_eval hits the threshold this tick
    store.save_status("g", GoalStatus(
        phase="in_flight", deliveries_since_eval=2,
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
    ))
    planner = FakeClaude(ACT_FEATURE)
    evaluator = FakeClaude(json.dumps({
        "verdict": "off_track", "rationale": "drifting from objective",
        "corrections": ["refocus on the API, not the UI"],
    }))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="did ui work"))
    notifier = RecordingNotifier()

    # delivery bumps 2→3; eval_every=3 → eval fires
    out = await _tick(store, "g", planner, evaluator, engine, notifier, eval_every=3)

    assert evaluator.calls == 1
    assert store.load_status("g").deliveries_since_eval == 0      # counter reset
    assert store.load_status("g").last_eval_verdict == "off_track"
    assert "refocus on the API" in store.recent_log("g") or \
        "refocus on the API" in (tmp_path / "g" / "inbox.md").read_text()
    # planner still ran afterward (momentum)
    assert planner.calls == 1
    assert out is Outcome.DISPATCHED


@pytest.mark.asyncio
async def test_midflight_eval_stalled_blocks(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", deliveries_since_eval=2,
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "x"),
    ))
    planner = FakeClaude(ACT_FEATURE)   # must NOT run — eval blocks first
    evaluator = FakeClaude(json.dumps({"verdict": "stalled", "rationale": "no real progress over 3 PRs"}))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="shipped but shallow"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, eval_every=3)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0
    assert store.load_status("g").phase == "blocked"


# ---- plain-language summarizer (owner messages rewritten; best-effort) ------


@pytest.mark.asyncio
async def test_owner_notification_is_plain_summarized(tmp_path, monkeypatch):
    """An OWNER-level message (a blocker) is rewritten by the summarizer before
    it reaches the notifier; the owner sees the plain text, not the raw line."""
    monkeypatch.delenv("DEVCLAW_NOTIFY_ALTITUDE", raising=False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()
    summarizer = RecordingSummarizer("🟡 I need you to pick how people sign in.")

    out = await _tick(store, "g", planner, evaluator, engine, notifier, summary_caller=summarizer)

    assert out is Outcome.BLOCKED
    assert len(summarizer.prompts) == 1                       # summarizer ran once
    assert "auth provider" in summarizer.prompts[0]           # raw line fed in
    assert notifier.sent == ["🟡 I need you to pick how people sign in."]  # plain text sent


@pytest.mark.asyncio
async def test_summarizer_not_invoked_for_suppressed_task_dispatch(tmp_path, monkeypatch):
    """A per-task dispatch is suppressed at the default floor — the summarizer
    must not be called for it (no wasted tokens on a message nobody sees)."""
    monkeypatch.delenv("DEVCLAW_NOTIFY_ALTITUDE", raising=False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    summarizer = RecordingSummarizer()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, summary_caller=summarizer)

    assert out is Outcome.DISPATCHED
    assert summarizer.prompts == []          # never summarized a suppressed message
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_idle_tick_never_invokes_summarizer(tmp_path, monkeypatch):
    """The zero-token guardrail extends to the summarizer: an idle tick must not
    call it."""
    monkeypatch.delenv("DEVCLAW_NOTIFY_ALTITUDE", raising=False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    summarizer = RecordingSummarizer()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, summary_caller=summarizer)

    assert out is Outcome.IDLE
    assert summarizer.prompts == []
    assert planner.calls == 0 and evaluator.calls == 0


# ---- grilling: async, durable, quota-safe scope alignment ------------------

ASK_DB = json.dumps({"action": "ask", "question": "Which database?", "recommended": "Postgres"})
GRILL_DONE = json.dumps({"action": "done", "spec": "Build a usable dashboard with auth and a home page."})


@pytest.mark.asyncio
async def test_grill_asks_first_question(tmp_path):
    """Entering grilling, the goal asks the first scope question (with a suggested
    answer) and waits — recording it as a pending transcript turn."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="grilling"))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    grill = FakeClaude(ASK_DB)

    out = await _tick(store, "g", planner, evaluator, engine, notifier, grill_caller=grill)

    assert out is Outcome.ASKED
    assert planner.calls == 0
    t = store.read_grill("g")
    assert len(t) == 1 and t[0]["question"] == "Which database?" and "answer" not in t[0]
    assert any("Which database?" in m and "Postgres" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_grill_waits_zero_tokens_for_reply(tmp_path):
    """A question is out and unanswered → the tick spends zero tokens (no grill
    cognition) until the owner replies."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="grilling"))
    store.write_grill("g", [{"question": "Which database?", "recommended": "Postgres"}])  # pending
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    grill = FakeClaude(ASK_DB)

    out = await _tick(store, "g", planner, evaluator, engine, notifier, grill_caller=grill)

    assert out is Outcome.IDLE
    assert grill.calls == 0 and planner.calls == 0          # the guardrail extends to the grill
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_grill_answer_finalizes_spec_into_plan_review(tmp_path):
    """Once the open question is answered, the next tick runs the grill again —
    here it finalizes: writes the spec and enters plan_review for approval."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="grilling"))
    store.write_grill("g", [{"question": "Which database?", "recommended": "Postgres", "answer": "Postgres"}])
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
    grill = FakeClaude(GRILL_DONE)

    out = await _tick(store, "g", planner, evaluator, engine, notifier, grill_caller=grill)

    assert out is Outcome.ASKED                              # now awaiting plan approval
    assert store.load_status("g").lifecycle == "plan_review"
    assert "usable dashboard" in store.read_spec("g")
    assert any("plan" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_plan_review_waits_then_approval_starts_executing(tmp_path):
    """plan_review spends zero tokens until approved; an approval flips it to
    executing."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="plan_review"))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    waiting = await _tick(store, "g", planner, evaluator, engine, notifier)
    assert waiting is Outcome.IDLE
    assert planner.calls == 0

    store.mark_plan_approved("g")
    out = await _tick(store, "g", planner, evaluator, engine, notifier)
    assert out is Outcome.SLEPT
    assert store.load_status("g").lifecycle == "executing"
    assert any("approved" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_discovery_enters_grilling_when_grill_enabled(tmp_path, monkeypatch):
    """With the grill on, a finished investigation flows into grilling (asking the
    first question) instead of straight to executing."""
    monkeypatch.setattr("devclaw.goal_tick._grill.GRILL_ENABLED", True)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    planner = FakeClaude(ACT)
    researcher = FakeClaude("## Current state\nbare API")   # evaluator-tier = discovery synthesis
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo analysis"))
    notifier, grill = RecordingNotifier(), FakeClaude(ASK_DB)

    out = await _tick(store, "g", planner, researcher, engine, notifier, grill_caller=grill)

    assert out is Outcome.ASKED
    assert store.load_status("g").lifecycle == "grilling"
    assert store.read_discovery("g")                        # brief still written
    assert any("Which database?" in m for m in notifier.sent)


# ---- auto-merge on gate-green (hands-off; gated + best-effort) --------------


def _delivery_status():
    return GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
    )


@pytest.mark.asyncio
async def test_green_delivery_auto_merges_when_enabled(tmp_path, monkeypatch):
    """A delivered change whose verify gate passed is merged by devclaw, with a
    plain owner ping — when DEVCLAW_GOAL_AUTOMERGE is on."""
    monkeypatch.setattr("devclaw.goal_tick._merge.AUTOMERGE_ENABLED", True)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", _delivery_status())
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    assert merger.merged == ["https://github.com/o/r/pull/9"]
    assert any("merged" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_failed_gate_is_not_auto_merged(tmp_path, monkeypatch):
    """A PR whose gate did NOT pass must never be auto-merged."""
    monkeypatch.setattr("devclaw.goal_tick._merge.AUTOMERGE_ENABLED", True)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", _delivery_status())
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="broke a test",
        pr_url="https://github.com/o/r/pull/9", gate_passed=False,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    assert merger.merged == []


@pytest.mark.asyncio
async def test_auto_merge_off_by_default(tmp_path):
    """With AUTOMERGE unset (default), even a green PR is left for manual review."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", _delivery_status())
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    assert merger.merged == []          # gated off → never merged


# ---- outcome lifecycle: investigate before executing -----------------------


@pytest.mark.asyncio
async def test_new_goal_opens_investigation(tmp_path):
    """A new outcome goal's first tick dispatches a read-only repo analysis and
    enters 'investigating' — it does NOT plan/act yet (research before acting)."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="new"))
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 0 and evaluator.calls == 0       # no cognition yet
    assert len(engine.dispatched) == 1
    action, _, _ = engine.dispatched[0]
    assert action.tool == "review_repository" and action.open_pr is False
    saved = store.load_status("g")
    assert saved.lifecycle == "investigating"
    assert saved.in_flight is not None and saved.in_flight.is_discovery is True
    assert any("look" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_investigation_running_is_zero_tokens(tmp_path):
    """While the discovery analysis runs, the tick costs zero tokens."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    planner, evaluator = FakeClaude(ACT), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(terminal=False, status="running"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IN_FLIGHT
    assert planner.calls == 0 and evaluator.calls == 0


@pytest.mark.asyncio
async def test_discovery_resolves_writes_brief_and_advances_to_executing(tmp_path):
    """When the analysis returns, the brief is synthesized + persisted, the owner
    is told, and the goal advances to 'executing'."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    planner = FakeClaude(ACT)  # must NOT run this tick
    # the research caller in tick_goal is the evaluator-tier caller:
    researcher = FakeClaude("## Current state\nbare API\n## Gap to good\nno UI\n## What good looks like\n- pages")
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo has 3 endpoints, no frontend"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, researcher, engine, notifier)

    assert out is Outcome.SLEPT
    assert researcher.calls == 1                              # the brief was synthesized
    assert "3 endpoints" in researcher.last_prompt           # repo analysis fed to synthesis
    assert planner.calls == 0
    assert store.load_status("g").lifecycle == "executing"   # advanced
    assert "Current state" in store.read_discovery("g")      # brief persisted
    assert any("look" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_discovery_synthesis_failure_still_advances(tmp_path):
    """A synthesis failure must never wedge a goal in investigation — it proceeds
    to executing anyway."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    planner = FakeClaude(ACT)
    researcher = FakeClaude("")   # empty → GoalResearchError inside synthesis
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="analysis"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, researcher, engine, notifier)

    assert out is Outcome.SLEPT
    assert store.load_status("g").lifecycle == "executing"   # not stuck
    assert notifier.sent                                     # owner still told


@pytest.mark.asyncio
async def test_legacy_goal_skips_investigation_and_plans(tmp_path):
    """A goal with no lifecycle (created before the front-end existed) behaves as
    'executing' — it plans + dispatches immediately, no discovery review."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")  # default status → lifecycle None
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 1                                 # planned (executing path)
    action, _, _ = engine.dispatched[0]
    assert action.tool == "start_program"                    # the planned action, NOT a discovery review


# ---- notification altitude (owner hears only owner-level by default) --------


@pytest.mark.asyncio
async def test_per_task_dispatch_is_suppressed_by_default(tmp_path, monkeypatch):
    """At the default 'owner' altitude the 🚀 per-task dispatch line — the spam a
    non-technical owner should never see — must NOT reach the notifier, even
    though the action is genuinely dispatched."""
    monkeypatch.delenv("DEVCLAW_NOTIFY_ALTITUDE", raising=False)  # default = owner
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED          # the action still ran
    assert len(engine.dispatched) == 1
    assert notifier.sent == []                # …but the owner heard nothing about it


@pytest.mark.asyncio
async def test_owner_level_blocker_always_sends(tmp_path, monkeypatch):
    """A real blocker (needs-you) is owner-altitude — it reaches the owner even at
    the default 'owner' floor."""
    monkeypatch.delenv("DEVCLAW_NOTIFY_ALTITUDE", raising=False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert any("auth provider" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_task_altitude_restores_the_firehose(tmp_path, monkeypatch):
    """DEVCLAW_NOTIFY_ALTITUDE=task is the debug firehose: the 🚀 per-task dispatch
    line is sent again."""
    monkeypatch.setenv("DEVCLAW_NOTIFY_ALTITUDE", "task")
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert any("🚀" in m for m in notifier.sent)


# ---- workspace-prep failure: block legibly, never silently loop -------------


async def _failing_prepare(workspace_dir: str, repo_url: str | None = None) -> str:
    """A prep that always fails the way a bad/missing/private repo_url does."""
    from devclaw.workspace import WorkspaceError

    raise WorkspaceError("clone failed: remote: Repository not found.")


async def _tick_prep(store, goal_id, planner, engine, notifier, *, prepare_ws):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(), notifier=notifier,
        notify_url="http://relay", prepare_ws=prepare_ws, eval_every=99,
    )


@pytest.mark.asyncio
async def test_executing_prep_failure_blocks_with_real_error(tmp_path):
    """A clone failure on the executing path must BLOCK with the git error as
    blocked_on (not drop to phase=idle and re-clone forever), and notify the
    owner — the regression that made a 1-char repo_url typo look like a dead goal."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()

    out = await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare)

    assert out is Outcome.BLOCKED
    st = store.load_status("g")
    assert st.phase == "blocked"
    assert "Repository not found" in (st.blocked_on or "")
    assert engine.dispatched == []                       # never ran the agent
    assert any("Repository not found" in m for m in notifier.sent)  # owner heard it


@pytest.mark.asyncio
async def test_investigation_prep_failure_blocks_without_cognition(tmp_path):
    """On a brand-new outcome goal the investigation prep is the SAME workspace
    executing needs — a prep failure there blocks immediately (lifecycle pinned to
    executing so future ticks route through the blocked-guard), spending zero
    planner tokens."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="new"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()

    out = await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare)

    assert out is Outcome.BLOCKED
    st = store.load_status("g")
    assert st.phase == "blocked" and st.lifecycle == "executing"
    assert "Repository not found" in (st.blocked_on or "")
    assert planner.calls == 0 and engine.dispatched == []


@pytest.mark.asyncio
async def test_blocked_on_prep_failure_does_not_respam(tmp_path):
    """After a prep-failure block, a cadence-only tick (no steering) stays idle
    and silent — one notification, not one per tick. Zero cognition."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()

    await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare)
    sent_after_block = len(notifier.sent)

    planner2 = FakeClaude(ACT)
    out = await _tick_prep(store, "g", planner2, engine, notifier, prepare_ws=_failing_prepare)

    assert out is Outcome.IDLE
    assert planner2.calls == 0
    assert len(notifier.sent) == sent_after_block        # no second ping
