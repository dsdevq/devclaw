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

from devclaw.goal.engine import GoalEngineError
from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, sweep_orphaned_refs, tick_all, tick_goal
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

ACT = json.dumps(
    {"decision": "act", "note": "ship next", "actions": [{"tool": "start_program", "goal": "build /health"}]}
)
ACT_FEATURE = json.dumps(
    {"decision": "act", "note": "feat", "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}]}
)


def _store(tmp_path, clock):
    return GoalStore(tmp_path, now=clock)


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, eval_every=99, verify_done=True, summary_caller=None, merger=None, remote_checker=None):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare,
        eval_every=eval_every, verify_done=verify_done, summary_caller=summary_caller,
        merger=merger, remote_checker=remote_checker,
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

    async def rec_prepare(ws, repo_url=None, branch=None, skills_required=None):
        calls.append((ws, repo_url, branch))
        return branch or "main"

    out = await tick_goal(
        "g", store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="", prepare_ws=rec_prepare, eval_every=99,
    )
    assert out is Outcome.DISPATCHED
    # seed_goal now sets a fake repo_url so the investigating phase takes the
    # repo-research path (vs world-research, which fires for from-scratch only).
    assert calls == [("/repos/demo", "https://example.com/demo.git", None)]  # legacy mode, no goal branch
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
    # Honest-wording contract (closeloop-bench 2026-07-05): the gate is named
    # as the SANDBOX gate (not CI), and the planner is told the PR's real
    # merge state instead of left to assume "gate=passed" means "merged".
    assert "sandbox gate=passed" in store.recent_log("g")
    assert "pr_state=open (unmerged — owner review pending)" in planner.last_prompt


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
async def test_dispatch_cap_lifts_in_checklist_mode(tmp_path):
    """When a checklist exists, the cap floor rises to the checklist size +
    margin — backlog size alone would block a long-checklist goal every few
    items (live-found 2026-06-26 on finance-sentry-mcp-v3: cap=7 from a
    5-item backlog blocked a goal with 22 ready items remaining)."""
    from devclaw.goal.models import Checklist, ChecklistItem

    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", backlog=["a", "b"])  # backlog cap would be 4

    # 20 atomic items in the checklist — the bounded work surface is much larger
    cl = Checklist(items=[
        ChecklistItem(id=f"i-{i}", requirement="r", evidence_target="t")
        for i in range(20)
    ])
    store.write_checklist("g", cl)

    # actions_dispatched=5 would trip the legacy backlog cap (=4) — but
    # checklist mode lifts the floor to 20+2=22, so this tick proceeds.
    store.save_status("g", GoalStatus(phase="idle", actions_dispatched=5))
    planner = FakeClaude(ACT_FEATURE)
    engine = FakeEngine()  # dispatch only
    out = await _tick(store, "g", planner, FakeClaude(), engine, RecordingNotifier())

    assert out is Outcome.DISPATCHED
    assert len(engine.dispatched) == 1


@pytest.mark.asyncio
async def test_dispatch_cap_still_blocks_when_checklist_exhausted(tmp_path):
    """The cap is checklist_size + small margin — a goal that's already
    dispatched more than every checklist item gets blocked, even in
    checklist mode (the planner is genuinely looping)."""
    from devclaw.goal.models import Checklist, ChecklistItem

    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", backlog=["a"])

    cl = Checklist(items=[
        ChecklistItem(id=f"i-{i}", requirement="r", evidence_target="t")
        for i in range(5)
    ])
    store.write_checklist("g", cl)
    # cap = max(1+2, 5+2) = 7; dispatched 7 → blocked
    store.save_status("g", GoalStatus(phase="idle", actions_dispatched=7))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", FakeClaude(ACT_FEATURE), FakeClaude(), FakeEngine(), notifier)

    assert out is Outcome.BLOCKED
    assert any("(7)" in m for m in notifier.sent)


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


SLEEP = json.dumps({"decision": "sleep", "note": "waiting"})


@pytest.mark.asyncio
async def test_verified_delivery_refunds_dispatch_cap(tmp_path):
    """A dispatch that settles done + gate-passed hands its cap budget back —
    the cap measures outstanding unproductive dispatches, not lifetime
    throughput, so an auto-merging mission goal never blocks on healthy work
    (live-found 2026-07-07: closeloop-mission-v2 blocked at cap 6 while
    shipping real merged PRs)."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight", actions_dispatched=4,
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))

    await _tick(store, "g", FakeClaude(SLEEP), FakeClaude(), engine, RecordingNotifier())

    assert store.load_status("g").actions_dispatched == 3


@pytest.mark.asyncio
async def test_gateless_successful_settle_refunds_dispatch_cap(tmp_path):
    """A gateless settle that succeeds (review, program) also refunds — a
    mission goal that verifies every delivery with a read-only review was
    structurally re-tripping the cap on healthy on_track work (live-found
    2026-07-09, closeloop-mission-v2). Only failures and gate-FAILED work
    accumulate."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight", actions_dispatched=4,
            in_flight=InFlight("devclaw", "review_repository", "t1", "task", "verify the delivery"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="repo analysis",
    ))

    await _tick(store, "g", FakeClaude(SLEEP), FakeClaude(), engine, RecordingNotifier())

    assert store.load_status("g").actions_dispatched == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poll",
    [
        # failed run — no refund
        PollResult(terminal=True, status="failed", detail="agent died"),
        # done but gate FAILED — unverified, no refund
        PollResult(terminal=True, status="done", detail="broke tests",
                   pr_url="https://github.com/o/r/pull/9", gate_passed=False),
    ],
)
async def test_unproductive_settle_keeps_dispatch_count(tmp_path, poll):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight", actions_dispatched=4,
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=poll)

    await _tick(store, "g", FakeClaude(SLEEP), FakeClaude(), engine, RecordingNotifier())

    assert store.load_status("g").actions_dispatched == 4


@pytest.mark.asyncio
async def test_refund_never_goes_negative(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight", actions_dispatched=0,
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))

    await _tick(store, "g", FakeClaude(SLEEP), FakeClaude(), engine, RecordingNotifier())

    assert store.load_status("g").actions_dispatched == 0


def test_steer_goal_resets_dispatch_counter_on_blocked(tmp_path):
    """steer_goal must zero actions_dispatched when unblocking so the dispatch
    cap doesn't re-fire on the very next tick after the human resolves the block."""
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    goals_dir = tmp_path / "goals"
    seed_goal(goals_dir, "g")

    db = StateStore(str(tmp_path / "state.db"))
    try:
        cfg = GoalConfig(goals_dir=goals_dir, notify_url="", tick_seconds=900, eval_every=99, verify_done=False)
        svc = GoalService(TaskQueue(db), db, config=cfg)
        # Seed + read back through the service's OWN store: since Tranche 1/PR3
        # status lives in the shared StateStore (not STATUS.md), so a separate
        # GoalStore over the same goals_dir would read its own private DB, not
        # the state the service mutated. (Matches test_cancel_goal.py.)
        svc._goal_store.save_status(
            "g", GoalStatus(phase="blocked", blocked_on="cap hit", actions_dispatched=5)
        )

        svc.steer_goal("g", "resume with new approach")

        saved = svc._goal_store.load_status("g")
        assert saved.phase == "idle"
        assert saved.actions_dispatched == 0
    finally:
        db.close()


# ---- resume_goal (the recovery verb — F7) -----------------------------------


def _resume_service(tmp_path):
    """A GoalService over the shared StateStore for resume_goal tests — mirrors
    test_steer_goal_resets_dispatch_counter_on_blocked's construction (status
    lives in the shared DB since Tranche 1, so tests must read back through the
    service's OWN store)."""
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    goals_dir = tmp_path / "goals"
    db = StateStore(str(tmp_path / "state.db"))
    cfg = GoalConfig(goals_dir=goals_dir, notify_url="", tick_seconds=900, eval_every=99, verify_done=False)
    return GoalService(TaskQueue(db), db, config=cfg), db, goals_dir


@pytest.mark.asyncio
async def test_resume_goal_unblocks_without_steering_and_replans_next_tick(tmp_path):
    """resume_goal is the recovery verb: it must fire UNBLOCK without appending
    a goal_steering row (a pure "blocker cleared" must never become a planner
    direction override — that was the F7 gap with steer_goal-as-only-unstick)
    AND guarantee a re-plan on the very next tick even with a fresh
    last_plan_at + a long cadence — a bare UNBLOCK would park the goal until
    cadence (should_plan = work OR cadence_due, and resume adds no work)."""
    svc, db, goals_dir = _resume_service(tmp_path)
    try:
        seed_goal(goals_dir, "g", cadence="30d")
        store = svc._goal_store
        store.save_status("g", GoalStatus(
            phase="blocked", blocked_on="sandbox image missing",
            actions_dispatched=5, last_plan_at=store.now_iso(),
        ))

        out = svc.resume_goal("g")

        assert out["resumed"] is True
        assert out["was_blocked_on"] == "sandbox image missing"
        saved = store.load_status("g")
        assert saved.phase == "idle"
        assert not saved.blocked_on                    # stale block reason cleared
        assert saved.actions_dispatched == 0           # cap won't re-fire on the first re-plan
        assert saved.last_plan_at is None              # cadence_due → True on the next tick
        assert store.unread_steering_rows("g") == []   # NO steering row appended

        planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()
        tick_out = await _tick(store, "g", planner, evaluator, engine, notifier)

        assert tick_out is Outcome.DISPATCHED          # re-planned despite fresh last_plan_at + 30d cadence
        assert planner.calls == 1
        assert "NEW steering" not in planner.last_prompt
    finally:
        db.close()


def test_resume_goal_noops_legibly_when_not_blocked(tmp_path):
    """No UNBLOCK edge exists from non-blocked states — resume_goal on an idle
    goal must no-op with a legible message, never raise IllegalTransition, and
    leave the status untouched (idempotency: a second resume is a no-op)."""
    svc, db, goals_dir = _resume_service(tmp_path)
    try:
        seed_goal(goals_dir, "g")
        svc._goal_store.save_status("g", GoalStatus(phase="idle", actions_dispatched=2))

        out = svc.resume_goal("g")

        assert out["resumed"] is False
        assert "not blocked" in out["message"]
        saved = svc._goal_store.load_status("g")
        assert saved.phase == "idle"
        assert saved.actions_dispatched == 2           # untouched — a true no-op
        assert svc._goal_store.unread_steering_rows("g") == []
    finally:
        db.close()


def test_resume_goal_refuses_firming_blocked_and_points_to_answer_unknowns(tmp_path):
    """A firming-blocked goal waits on owner answers only answer_unknowns can
    supply — a bare unblock would strand it in FIRMING_IDLE limbo (round-1
    firming already wrote the draft, so FirmingHandler.can_run stays False and
    no event ever fires). resume_goal must refuse WITHOUT transitioning."""
    svc, db, goals_dir = _resume_service(tmp_path)
    try:
        seed_goal(goals_dir, "g")
        svc._goal_store.save_status("g", GoalStatus(
            phase="blocked", lifecycle="firming", blocked_on="2 unknowns need owner answers",
        ))

        out = svc.resume_goal("g")

        assert out["resumed"] is False
        assert "answer_unknowns" in out["message"]
        saved = svc._goal_store.load_status("g")
        assert saved.phase == "blocked" and saved.lifecycle == "firming"   # no transition fired
        assert saved.blocked_on == "2 unknowns need owner answers"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_blocked_goal_costs_zero_cognition_until_resume_goal(tmp_path):
    """The zero-token guard holds around the new verb: a blocked goal costs 0
    planner/evaluator calls tick after tick, and only resume_goal reopens the
    cognition path."""
    svc, db, goals_dir = _resume_service(tmp_path)
    try:
        seed_goal(goals_dir, "g", cadence="30d")
        store = svc._goal_store
        store.save_status("g", GoalStatus(
            phase="blocked", blocked_on="cap hit", last_plan_at=store.now_iso(),
        ))
        planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

        for _ in range(3):
            out = await _tick(store, "g", planner, evaluator, engine, notifier)
            assert out is Outcome.IDLE
        assert planner.calls == 0 and evaluator.calls == 0   # blocked = 0 tokens

        svc.resume_goal("g")
        out = await _tick(store, "g", planner, evaluator, engine, notifier)

        assert out is Outcome.DISPATCHED
        assert planner.calls == 1
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
    # The dispatched review brief MUST carry the strict per-clause directive — this
    # is what closes the 2026-06-25 "stub-everything passed the done-gate" failure
    # mode by ensuring both the reviewer (inside the sandbox) and the direction
    # evaluator (in devclaw) speak the same per-clause-evidence vocabulary.
    brief = review_action.goal
    assert "DECOMPOSE" in brief and "atomic clauses" in brief
    assert "Per-clause evidence" in brief
    assert "not_yet_available" in brief or "stub" in brief.lower()  # the failure-mode warning
    assert "Objective:" in brief and "Done when:" in brief
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
    evaluator = FakeClaude(json.dumps({
        "verdict": "achieved",
        "rationale": "/health exists and is tested",
        "clauses": [
            {
                "clause": "/health returns 200",
                "satisfied": True,
                "evidence": "src/Health.cs:12 returns OK; HealthTests.cs:8 asserts 200",
            },
            {
                "clause": "/health is tested",
                "satisfied": True,
                "evidence": "HealthTests.cs:8 Health_Returns200",
            },
        ],
    }))
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
    assert "add a test for /health" in store.unread_steering("g")


@pytest.mark.asyncio
async def test_done_gate_disabled_uses_artifact_eval(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    planner = FakeClaude(json.dumps({"decision": "done", "note": "done"}))
    evaluator = FakeClaude(json.dumps({
        "verdict": "achieved",
        "rationale": "deliveries show done_when met",
        "clauses": [
            {
                "clause": "/health returns 200",
                "satisfied": True,
                "evidence": "PR #1 added src/Health.cs:12 + HealthTests.cs:8",
            },
            {
                "clause": "/health is tested",
                "satisfied": True,
                "evidence": "HealthTests.cs:8 Health_Returns200 passing",
            },
        ],
    }))
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


@pytest.mark.asyncio
async def test_discovery_goes_straight_to_executing(tmp_path):
    """Scope alignment is owned by the OpenClaw waiter (scope_grill MCP tool) —
    when investigation finishes, the chef writes the discovery brief and steps
    directly into executing, with no in-chef grill phase in between."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    planner = FakeClaude(ACT)
    researcher = FakeClaude("## Current state\nbare API")   # evaluator-tier = discovery synthesis
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo analysis"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, researcher, engine, notifier)

    assert out is Outcome.ADVANCED
    assert store.load_status("g").lifecycle == "executing"
    assert store.read_discovery("g")                        # brief still written


# ---- auto-merge on gate-green (hands-off; gated + best-effort) --------------


def _delivery_status():
    return GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
    )


@pytest.mark.asyncio
async def test_green_delivery_auto_merges_when_enabled(tmp_path, monkeypatch):
    """A delivered change whose verify gate passed is merged by devclaw and a
    TASK-altitude ping is emitted — when the automerge default is on.
    The ping is TASK-altitude (not OWNER) so per-PR merges don't spam the owner
    on a goal that lands many PRs; drop the floor so this test can observe it."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    monkeypatch.setenv("DEVCLAW_NOTIFY_ALTITUDE", "task")
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
    # The planner's finished-detail reflects the merge that just happened —
    # built AFTER the auto-merge attempt, not before.
    assert "pr_state=merged" in planner.last_prompt


@pytest.mark.asyncio
async def test_merge_fires_on_a_passed_merger_even_with_global_flag_off(tmp_path, monkeypatch):
    """Regression lock for the per-project-override bug found 2026-07-05: a
    project can pin automerge ON for its own repo even while the devclaw-wide
    devclaw-wide automerge default is off. GoalService resolves that into an
    actual merger callable (or None) and hands it to the tick — this proves
    the tick honors WHATEVER merger it's given and does not independently
    re-check the raw global flag, which would silently override a project's
    explicit 'on' with the fleet default."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", _delivery_status())
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    # A project override resolved this ON despite the global default being off —
    # simulated here by simply handing tick_goal a real merger regardless of flag.
    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    assert merger.merged == ["https://github.com/o/r/pull/9"]


@pytest.mark.asyncio
async def test_program_settle_reconciles_pr_stack(tmp_path, monkeypatch):
    """A finished program leaves a STACK of PRs no single gate verdict covers
    (gate_passed=None), so the single-PR auto-merge can't touch them — the
    goal used to burn follow-up dispatches shepherding its own PRs and left
    zombies behind (2026-07-09: five open superseded closeloop PRs). The
    settle hook must reconcile the stack in order and feed the REAL per-PR
    outcome to the planner instead of 'unmerged — owner review pending'."""
    calls = {}

    async def fake_reconcile(stack, *, workspace_dir, merger):
        calls["stack"] = stack
        return [f"{u}: merged" for u in stack]

    monkeypatch.setattr("devclaw.goal.reconcile.reconcile_stack", fake_reconcile)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "start_program", "p1", "program", "ship CI/CD"),
    ))
    planner, evaluator = FakeClaude(SLEEP), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="program done",
        pr_url="https://github.com/o/r/pull/66; https://github.com/o/r/pull/67",
        gate_passed=None,
    ))

    await _tick(store, "g", planner, evaluator, engine, RecordingNotifier(), merger=RecordingMerger())

    assert calls["stack"] == [
        "https://github.com/o/r/pull/66", "https://github.com/o/r/pull/67",
    ]
    assert "pr_stack reconciled" in planner.last_prompt
    log = (tmp_path / "g" / "log.md").read_text()
    assert "reconcile: https://github.com/o/r/pull/66: merged" in log


@pytest.mark.asyncio
async def test_program_settle_without_merger_leaves_stack_alone(tmp_path, monkeypatch):
    """Automerge off (no merger resolved) → the reconcile step must not run:
    the owner reviews program PRs by hand, same contract as single-task
    auto-merge."""
    async def boom(stack, *, workspace_dir, merger):  # pragma: no cover - must not run
        raise AssertionError("reconcile must not run without a merger")

    monkeypatch.setattr("devclaw.goal.reconcile.reconcile_stack", boom)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "start_program", "p1", "program", "ship CI/CD"),
    ))
    planner = FakeClaude(SLEEP)
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="program done",
        pr_url="https://github.com/o/r/pull/66", gate_passed=None,
    ))

    await _tick(store, "g", planner, FakeClaude(), engine, RecordingNotifier(), merger=None)

    assert "pr_state=open (unmerged — owner review pending)" in planner.last_prompt


@pytest.mark.asyncio
async def test_checklist_mode_dispatch_is_not_auto_merged(tmp_path, monkeypatch):
    """Pillar 2 invariant: when the settled action carries checklist
    addresses, its PR is the SHARED goal-branch PR every item keeps pushing
    to. Auto-merging it now would delete the goal branch and force item N+1
    to re-fork from main — the 2026-06-26 finance-sentry-mcp-v4 regression.
    Auto-merge is skipped in that case; the done-gate is the single review
    moment for the cumulative work."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    # In-flight ref carries checklist addresses (set by the dispatch hook).
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "implement_feature", "t1", "task", "add /health",
            addresses=["scaffold"],  # ← Pillar 1 marker
        ),
    ))
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="ok",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    # Merger never invoked — the PR is the shared goal-branch PR.
    assert merger.merged == []
    # No "merged" owner ping either.
    assert not any("merged" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_legacy_dispatch_without_addresses_still_auto_merges(tmp_path, monkeypatch):
    """Backwards-compat: legacy backlog-mode goals (no addresses on the
    in-flight ref) keep the existing auto-merge behaviour. Only Pillar 1
    checklist dispatches skip the merge."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "implement_feature", "t1", "task", "add /health",
            addresses=[],  # ← legacy mode, no checklist
        ),
    ))
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="ok",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier, merger = RecordingNotifier(), RecordingMerger()

    await _tick(store, "g", planner, evaluator, engine, notifier, merger=merger)

    assert merger.merged == ["https://github.com/o/r/pull/9"]


@pytest.mark.asyncio
async def test_failed_gate_is_not_auto_merged(tmp_path, monkeypatch):
    """A PR whose gate did NOT pass must never be auto-merged."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
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
    """With automerge disabled, no merger is ever passed down to a tick in the
    first place — the enabled/disabled decision is resolved ONCE, by
    GoalService._merger (project override, else the devclaw-wide
    merge.AUTOMERGE_ENABLED default; see devclaw.goal.merge.resolve_automerge),
    before a merger callable is even constructed. This tick layer's own
    contract is simpler and absolute: given no merger (``merger=None``, what
    GoalService actually produces when automerge is off), never attempt to
    merge — it must not independently re-check any global flag itself, since
    that would prevent a project's override from ever turning merging ON
    against an off-by-default fleet (or OFF against an on-by-default one)."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", _delivery_status())
    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, merger=None)

    assert out is Outcome.DISPATCHED  # settled + planned the next action normally
    assert not any("merged" in m.lower() for m in notifier.sent)


@pytest.mark.asyncio
async def test_tick_all_resolves_merger_per_goal(tmp_path, monkeypatch):
    """tick_all sweeps every goal in ONE pass. A project's automerge override
    for one goal must not leak onto another goal in the same sweep — this is
    what merger_resolver is for (GoalService._merger_resolver binds it to a
    per-project lookup; see devclaw.goal.merge.resolve_automerge). Prove
    tick_all actually calls the resolver fresh per goal_id rather than
    resolving a single merger once for the whole fleet."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", False)
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "on", workspace_dir="/repos/on")
    seed_goal(tmp_path, "off", workspace_dir="/repos/off")
    store.save_status("on", _delivery_status())
    store.save_status("off", _delivery_status())

    on_merger = RecordingMerger()

    def _resolver(goal):
        return on_merger if goal.workspace_dir == "/repos/on" else None

    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
    ))
    notifier = RecordingNotifier()

    await tick_all(
        store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="http://relay", prepare_ws=fake_prepare,
        merger_resolver=_resolver,
    )

    assert on_merger.merged == ["https://github.com/o/r/pull/9"]  # "on" project merged
    # "off" project resolved to no merger — nothing else was merged anywhere.


@pytest.mark.asyncio
async def test_tick_all_resolves_verify_done_per_goal(tmp_path):
    """Same per-goal-freshness contract as the merger, for the done-gate
    re-check flag: tick_all must call verify_done_resolver once per goal (so a
    project's verify_done override can't leak onto another goal in the sweep),
    not resolve one fleet-wide value. Prove the resolver is invoked per goal_id."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "a", workspace_dir="/repos/a")
    seed_goal(tmp_path, "b", workspace_dir="/repos/b")
    store.save_status("a", GoalStatus(lifecycle="investigating"))
    store.save_status("b", GoalStatus(lifecycle="investigating"))

    seen: list[str] = []

    def _vd_resolver(goal):
        seen.append(goal.workspace_dir)
        return goal.workspace_dir == "/repos/a"  # per-goal, distinct values

    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    await tick_all(
        store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="http://relay", prepare_ws=fake_prepare,
        verify_done_resolver=_vd_resolver,
    )

    assert sorted(seen) == ["/repos/a", "/repos/b"]  # called fresh per goal


@pytest.mark.asyncio
async def test_tick_all_resolves_autodeploy_per_goal(tmp_path):
    """autodeploy gets the same per-goal-freshness treatment: tick_all calls
    autodeploy_resolver once per goal so a project's autodeploy override can't
    leak onto another goal in the sweep."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "a", workspace_dir="/repos/a")
    seed_goal(tmp_path, "b", workspace_dir="/repos/b")
    store.save_status("a", GoalStatus(lifecycle="investigating"))
    store.save_status("b", GoalStatus(lifecycle="investigating"))

    seen: list[str] = []

    def _ad_resolver(goal):
        seen.append(goal.workspace_dir)
        return goal.workspace_dir == "/repos/a"

    planner, evaluator = FakeClaude(ACT_FEATURE), FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    await tick_all(
        store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="http://relay", prepare_ws=fake_prepare,
        autodeploy_resolver=_ad_resolver,
    )

    assert sorted(seen) == ["/repos/a", "/repos/b"]


@pytest.mark.asyncio
async def test_auto_deploy_disabled_returns_empty_without_deploying(tmp_path):
    """_auto_deploy honors its resolved `enabled` flag (no longer the env var):
    enabled=False short-circuits to '' before any deploy attempt."""
    from devclaw.goal.tick import _auto_deploy

    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", workspace_dir="/repos/g")
    goal = store.load_goal("g")
    out = await _auto_deploy("g", goal, store, enabled=False)
    assert out == ""


# ---- outcome lifecycle: investigate before executing -----------------------


@pytest.mark.asyncio
async def test_new_goal_opens_investigation(tmp_path):
    """A new outcome goal's first tick dispatches a read-only repo analysis and
    enters 'investigating' — it does NOT plan/act yet (research before acting)."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="investigating"))
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

    assert out is Outcome.ADVANCED
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

    assert out is Outcome.ADVANCED
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


async def _failing_prepare(
    workspace_dir: str, repo_url: str | None = None, branch: str | None = None, skills_required: list[str] | None = None,
) -> str:
    """A prep that always fails the way a bad/missing/private repo_url does."""
    from devclaw.engine.workspace import WorkspaceError

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
    store.save_status("g", GoalStatus(phase="idle", lifecycle="investigating"))
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


# ---- regression: the duplicate-ship loop (dogfood 2026-06-21) ---------------


class RaisingClaude:
    """A cognition caller that always raises — models the planner hitting a usage
    limit (or any error) right after an action finished."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        raise self.exc


@pytest.mark.asyncio
async def test_consumed_action_is_persisted_before_a_planner_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    # The bug: in_flight=None was computed in memory but NOT saved before the
    # next-action planner ran; the planner crashing on a usage limit aborted the
    # tick with the stale pointer on disk, so the next tick re-shipped/re-announced
    # the same finished action forever. A non-(Goal)PlannerError still escapes
    # tick_goal — but the cleared state must already be durable.
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "build M2\n\nlong body"),
    ))
    planner = RaisingClaude(RuntimeError("boom after the action finished"))
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="done",
        pr_url="https://github.com/o/r/pull/2", gate_passed=True,
    ))
    notifier = RecordingNotifier()
    merger = RecordingMerger(ok=True)

    with pytest.raises(RuntimeError):
        await _tick(store, "g", planner, FakeClaude(), engine, notifier, merger=merger)

    # the action was consumed + merged exactly once …
    assert merger.merged == ["https://github.com/o/r/pull/2"]
    # … and the cleared in_flight is DURABLE despite the crash — so a re-tick
    # plans the next milestone instead of re-shipping this one.
    assert store.load_status("g").in_flight is None


@pytest.mark.asyncio
async def test_planner_session_limit_is_caught_not_escaped(tmp_path):
    # The shared `claude --print` caller raises planner.PlannerError (NOT
    # GoalPlannerError) on a usage limit. goal_tick must catch it (so it can pause
    # / handle it) rather than let it escape to the outer 'tick error (isolated)'.
    from devclaw.planner import PlannerError
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "build M2"),
    ))
    planner = RaisingClaude(PlannerError("You've hit your session limit · resets 12:20am (Europe/Dublin)"))
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="done",
        pr_url="https://github.com/o/r/pull/2", gate_passed=True,
    ))

    out = await _tick(store, "g", planner, FakeClaude(), engine, RecordingNotifier(), merger=RecordingMerger())

    # caught + handled (FakeEngine has no set_global_pause → ERROR, but NOT raised)
    assert out is Outcome.ERROR
    assert store.load_status("g").in_flight is None  # still durably cleared


@pytest.mark.asyncio
async def test_ship_notification_is_concise_not_the_full_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    # shipped+merged is TASK-altitude (per-PR chatter, not owner-altitude) — drop
    # the floor so this test can observe it.
    monkeypatch.setenv("DEVCLAW_NOTIFY_ALTITUDE", "task")
    # The notification must not paste the action's full instruction prompt (which
    # is what happened when the plain-language summarizer was quota-blocked and
    # fell back to raw text). With summary_caller=None the raw text is sent — and
    # it must already be terse.
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    long_goal = "Implement M2 deals CRUD\n\nSECRET_DETAIL_LINE that must never be pasted into a ping\n- a\n- b"
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", long_goal),
    ))
    planner = FakeClaude(ACT_FEATURE)
    notifier = RecordingNotifier()

    await _tick(store, "g", planner, FakeClaude(), FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="done",
        pr_url="https://github.com/o/r/pull/2", gate_passed=True,
    )), notifier, merger=RecordingMerger())

    ship = [m for m in notifier.sent if "shipped + merged" in m]
    assert ship, "expected a shipped+merged notification"
    assert "SECRET_DETAIL_LINE" not in ship[0]      # not the full prompt
    assert len(ship[0]) < 160                        # terse


@pytest.mark.asyncio
async def test_ship_notification_is_suppressed_at_owner_altitude(tmp_path, monkeypatch):
    """Per-PR shipped+merged is TASK-altitude, so at the default OWNER floor it
    must not reach the owner (otherwise a goal that lands 10 PRs in an afternoon
    fires 10 owner-altitude "✅ complete" pings — the dogfood incident that
    demoted this notification)."""
    monkeypatch.setattr("devclaw.goal.tick._merge.AUTOMERGE_ENABLED", True)
    monkeypatch.setenv("DEVCLAW_NOTIFY_ALTITUDE", "owner")  # the default; explicit for the test
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "short goal"),
    ))
    notifier = RecordingNotifier()

    await _tick(store, "g", FakeClaude(ACT_FEATURE), FakeClaude(), FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="done",
        pr_url="https://github.com/o/r/pull/2", gate_passed=True,
    )), notifier, merger=RecordingMerger())

    assert not [m for m in notifier.sent if "shipped + merged" in m], \
        "shipped+merged must not reach owner altitude"


# ---- done-gate review brief — the two-axis structural fix -------------------


def test_done_gate_review_brief_carries_both_axes(tmp_path):
    """The brief must instruct the reviewer to grade TWO axes — functional
    clauses AND structural health. Without the second axis, four PRs can each
    satisfy the clauses while compounding bloat (closeloop App.tsx 1153 →
    1827 LOC). The brief now demands a ``## Structural health`` section."""
    from devclaw.goal.tick import _done_gate_review_brief
    seed_goal(tmp_path, "g")
    store = _store(tmp_path, Clock())
    goal = store.load_goal("g")
    brief = _done_gate_review_brief(goal)
    # functional axis (pre-existing)
    assert "## Per-clause evidence" in brief
    # structural axis (new)
    assert "## Structural health" in brief
    assert "verdict: clean | concerns | poor" in brief
    # the "what would a senior engineer do" framing — agency, not rules
    assert "senior engineer" in brief.lower()
    # the failure modes the structural section must catch
    body = brief.lower()
    for smell in ("god object", "untested behaviour", "no-op stub"):
        assert smell in body, f"structural section should name {smell!r} as a thing to catch"
    # the summary must speak to BOTH axes — not just clauses
    assert "BOTH axes" in brief or "both axes" in brief or "covering BOTH" in brief


# ---- grounded remote-checks at the done-gate (2026-07-06 benchmark fix) -----
#
# Backstory: closeloop-bench-2026-07-05 closed `achieved` while all 32 GitHub
# Actions runs on its goal branch were startup_failure — the sandbox gate was
# green and nothing ever looked at the repo's real check surface. When a
# remote checker is bound and the goal works on a shared goal branch, an
# `achieved` must survive the real CI state before the goal closes.

_ACHIEVED_EVAL = json.dumps({
    "verdict": "achieved",
    "rationale": "all clauses met",
    "clauses": [
        {"clause": "/health returns 200", "satisfied": True,
         "evidence": "src/Health.cs:12; HealthTests.cs:8 passes"},
    ],
    "structural_health": "clean",
})


class FakeRemoteChecker:
    """Records (repo_url, branch) calls; returns a canned result or raises."""

    def __init__(self, result=None, exc: Exception | None = None):
        from devclaw.goal.remote_checks import RemoteChecksResult

        self.result = result or RemoteChecksResult("passing", "all green")
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, repo_url: str, branch: str):
        self.calls.append((repo_url, branch))
        if self.exc:
            raise self.exc
        return self.result


def _verifying_checklist_goal(store, tmp_path, goal_id="g"):
    """Seed a checklist-mode goal parked at the done-gate review settle."""
    from devclaw.goal.models import Checklist, ChecklistItem

    seed_goal(tmp_path, goal_id)
    store.write_checklist(goal_id, Checklist(items=[
        ChecklistItem(
            id="scaffold", requirement="Create the csproj.",
            evidence_target="backend/src/Foo.csproj",
            addresses_files=["backend/src/Foo.csproj"], status="done",
        ),
    ]))
    store.save_status(goal_id, GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))


@pytest.mark.asyncio
async def test_failing_remote_checks_block_the_close(tmp_path):
    from devclaw.goal.remote_checks import RemoteChecksResult

    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker(RemoteChecksResult("failing", "32 failed of 32 (32× startup_failure)"))
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.SLEPT                       # not done — steered back in
    assert checker.calls == [("https://example.com/demo.git", "goal/g")]
    s = store.load_status("g")
    assert s.phase == "idle" and s.phase != "done"
    assert s.last_eval_verdict == "off_track"
    assert "remote checks (goal/g): failing" in store.recent_log("g")
    # the correction steers the fix
    steering = store.unread_steering("g")
    assert "[remote-checks]" in steering
    assert "startup_failure" in steering


@pytest.mark.asyncio
async def test_never_ran_ci_blocks_the_close_under_strict_gate(tmp_path, monkeypatch):
    from devclaw.goal import remote_checks as _rc
    from devclaw.goal.remote_checks import RemoteChecksResult

    monkeypatch.setattr(_rc, "CI_GATE_MODE", "strict")
    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker(RemoteChecksResult("none", "workflows exist but zero runs"))
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.SLEPT
    assert store.load_status("g").phase != "done"
    steering = store.unread_steering("g")
    assert "ZERO" in steering or "zero" in steering


@pytest.mark.asyncio
async def test_broken_ci_infra_closes_with_annotation_under_flexible_gate(tmp_path):
    # Default (flexible) ci-gate: startup_failure-only CI is infrastructure
    # trouble, not code trouble — the verified close is honored, but the
    # verdict the owner reads says loudly that CI never executed.
    from devclaw.goal.remote_checks import RemoteChecksResult

    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker(
        RemoteChecksResult("infra_broken", "5 of 5 died at startup — CI infrastructure never executed")
    )
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.DONE
    assert store.load_status("g").phase == "done"
    assert "internal verify gate only" in store.load_status("g").last_eval_note


@pytest.mark.asyncio
async def test_passing_remote_checks_let_the_goal_close(tmp_path):
    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker()  # passing
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.DONE
    assert store.load_status("g").phase == "done"
    assert "remote checks (goal/g): passing" in store.recent_log("g")


@pytest.mark.asyncio
async def test_unknown_remote_state_fails_open_but_logs(tmp_path):
    from devclaw.goal.remote_checks import RemoteChecksResult

    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker(RemoteChecksResult("unknown", "gh: network unreachable"))
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    # infra uncertainty must not wedge a verified goal — but it IS observable
    assert out is Outcome.DONE
    assert "remote checks (goal/g): unknown" in store.recent_log("g")


@pytest.mark.asyncio
async def test_checker_exception_fails_open(tmp_path):
    store = _store(tmp_path, Clock())
    _verifying_checklist_goal(store, tmp_path)
    checker = FakeRemoteChecker(exc=RuntimeError("gh exploded"))
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.DONE
    assert "unknown" in store.recent_log("g")


@pytest.mark.asyncio
async def test_legacy_goal_without_checklist_skips_the_checker(tmp_path):
    # No shared goal branch → nothing meaningful to check; behaviour unchanged.
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))
    checker = FakeRemoteChecker()
    planner, evaluator = FakeClaude(ACT), FakeClaude(_ACHIEVED_EVAL)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, remote_checker=checker)

    assert out is Outcome.DONE
    assert checker.calls == []


def test_done_gate_review_brief_forbids_existence_only_test_evidence(tmp_path):
    """The in-sandbox reviewer must be told the same rule the evaluator
    enforces: spec files existing ≠ tests passing (closeloop-bench-2026-07-05
    shipped a verify.sh that only grepped for the Playwright files)."""
    from devclaw.goal.tick import _done_gate_review_brief
    from devclaw.goal.models import Goal

    brief = _done_gate_review_brief(Goal(
        id="g", objective="o", cadence="1d", engine="devclaw", workspace_dir="/ws",
        done_when="the flow is tested end to end",
    ))
    assert "merely EXIST" in brief
    assert "does NOT satisfy" in brief


# ---- standing-goal done-gate (the 2026-07-06 benchmark fix) -----------------


@pytest.mark.asyncio
async def test_standing_goal_done_gate_blocks_instead_of_closing(tmp_path):
    """closeloop-bench-2026-07-05: a done_when that declares the goal STANDING
    ("not a bounded criterion") must never terminally close via the done-gate.
    An all-axes-pass review becomes needs_human → the goal BLOCKS and the owner
    gets the close-or-steer decision; phase stays out of 'done'."""
    store = _store(tmp_path, Clock())
    seed_goal(
        tmp_path, "g",
        done_when=(
            "Not applicable as a bounded criterion — this is a standing goal. "
            "Judge each delivery against the four axes; fail any → off_track."
        ),
    )
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))
    planner = FakeClaude(ACT)  # must NOT be called
    evaluator = FakeClaude(json.dumps({
        "verdict": "achieved",
        "rationale": "every axis passes",
        "clauses": [
            {"clause": "research is real", "satisfied": True, "evidence": "docs/research/crm.md"},
        ],
        "structural_health": "clean",
    }))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="review ok"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert s.phase != "done"
    assert "standing" in (s.blocked_on or "").lower()
    # the evaluator prompt carried the contract note
    assert "STANDING-GOAL CONTRACT" in evaluator.last_prompt
    # owner was told, not bypassed
    assert any("standing" in m.lower() for m in notifier.sent)


# ---- orphaned-ref sweep (2026-07-09 lost-in-flight-ref incident) -----------
#
# PR7 demoted the per-tick reconcile (which lived HERE, inside tick_goal) to
# a once-per-service-start sweep (sweep_orphaned_refs) — atomic dispatch
# makes losing a ref mid-flight structurally impossible on THIS build going
# forward, so a per-tick check is no longer load-bearing; the sweep still
# catches a ref lost by an older build or something outside the dispatch
# path. These three tests are MECHANICALLY re-pointed at the sweep: same
# scenarios, same assertions, split across an explicit sweep_orphaned_refs()
# call (what used to happen silently inside the one tick_goal call) followed
# by an ordinary tick (which now finds the ref already restored and proceeds
# exactly as it always has for an in-flight action).


class OrphanAwareEngine(FakeEngine):
    """FakeEngine + the latest_program_for_goal finder the sweep probes."""

    def __init__(self, *, program: "tuple[str, str] | None" = None, **kw):
        super().__init__(**kw)
        self.program = program

    def latest_program_for_goal(self, goal_id: str):
        return self.program


@pytest.mark.asyncio
async def test_orphaned_failed_program_readopted_and_settled(tmp_path):
    """A goal whose in_flight ref was lost (STATUS.md truncated by a crash
    mid-write) must have the SWEEP rediscover its own already-failed program
    via parent_goal_id and re-adopt it; the NEXT ordinary tick then settles
    it and replans WITH the failure as input — instead of idling forever on
    a result that will never arrive."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    # cadence not due + no steering → without the sweep this tick is IDLE
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    store.append_log("g", "dispatched start_program: Program: reporting & dashboards")
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = OrphanAwareEngine(
        program=("p_lost", "Program: reporting & dashboards"),
        poll_result=PollResult(
            terminal=True, status="failed",
            detail="program failed — task exceeded the 1800s wall-clock timeout",
        ),
    )

    swept = await sweep_orphaned_refs(store, engine)

    assert swept == {"g": "program p_lost"}
    log = store.recent_log("g")
    assert "re-adopted orphaned program p_lost" in log
    s = store.load_status("g")
    assert s.in_flight is not None and s.in_flight.id == "p_lost"

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED
    assert planner.calls == 1
    assert "wall-clock timeout" in planner.last_prompt  # failure fed to the planner
    log = store.recent_log("g")
    assert "start_program p_lost → failed" in log


@pytest.mark.asyncio
async def test_settled_program_is_not_readopted(tmp_path):
    """A program whose result already reached log.md must NOT be re-adopted —
    the sweep only rescues lost refs, it never replays settled work."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    store.append_log("g", "start_program p_seen → failed")
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = OrphanAwareEngine(program=("p_seen", "Program: reporting"))

    swept = await sweep_orphaned_refs(store, engine)

    assert swept == {}
    assert engine.polls == 0

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IDLE
    assert planner.calls == 0
    assert engine.polls == 0


@pytest.mark.asyncio
async def test_orphaned_running_program_readopted_as_in_flight(tmp_path):
    """A lost ref to a STILL-RUNNING program is restored by the sweep; the
    next tick then reports IN_FLIGHT — zero cognition spent, and the
    following settle works normally."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = OrphanAwareEngine(
        program=("p_run", "Program: reporting"),
        poll_result=PollResult(terminal=False, status="running"),
    )

    swept = await sweep_orphaned_refs(store, engine)

    assert swept == {"g": "program p_run"}
    s = store.load_status("g")
    assert s.in_flight is not None and s.in_flight.id == "p_run"
    assert s.in_flight.ref_kind == "program"

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.IN_FLIGHT
    assert planner.calls == 0
    s = store.load_status("g")
    assert s.in_flight is not None and s.in_flight.id == "p_run"
    assert s.in_flight.ref_kind == "program"


@pytest.mark.asyncio
async def test_save_status_is_atomic_replace(tmp_path):
    """save_status must never leave a partial STATUS.md or a stray tmp file —
    the file is the only link to in-flight work."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "start_program", "p1", "program")),
    )
    d = tmp_path / "g"
    assert not (d / "STATUS.md.tmp").exists()
    assert store.load_status("g").in_flight.id == "p1"



# ---- lost in-flight ref: block legibly, never wedge (audit 2026-07-10) ------
# The engine row a ref points at can vanish (DB loss, manual cleanup, a
# cross-environment restore). poll then raises GoalEngineError on EVERY tick,
# and tick_all's catch-all logs "tick error (isolated)" without clearing
# in_flight — a silent, permanent error loop. These tests pin the guard: one
# BLOCKED tick that clears the ref, one owner ping, then zero-token idle.


@pytest.mark.asyncio
async def test_lost_action_ref_blocks_and_notifies_owner(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t_gone", "task", "add /health"),
    ))
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = FakeEngine(poll_exc=GoalEngineError("unknown task_id: t_gone"))

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    st = store.load_status("g")
    assert st.phase == "blocked"
    assert st.in_flight is None                          # the lost ref is cleared
    assert "task t_gone" in (st.blocked_on or "")
    assert "unknown task_id" in (st.blocked_on or "")    # the real error, not a paraphrase
    assert planner.calls == 0 and evaluator.calls == 0   # zero cognition on the failure path
    assert len(notifier.sent) == 1                       # owner heard it exactly once
    assert "t_gone" in notifier.sent[0]
    assert "t_gone" in store.recent_log("g")


@pytest.mark.asyncio
async def test_lost_ref_block_does_not_loop(tmp_path):
    """The wedge regression proper: the NEXT tick after a lost-ref block must
    idle at 0 tokens (no re-poll, no re-raise, no second ping) — cadence never
    re-pokes a blocked goal, only steering does."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t_gone", "task", "add /health"),
    ))
    notifier = RecordingNotifier()
    engine = FakeEngine(poll_exc=GoalEngineError("unknown task_id: t_gone"))
    await _tick(store, "g", FakeClaude(ACT), FakeClaude(), engine, notifier)
    sent_after_block = len(notifier.sent)

    planner2, evaluator2 = FakeClaude(ACT), FakeClaude()
    out = await _tick(store, "g", planner2, evaluator2, engine, notifier)

    assert out is Outcome.IDLE
    assert planner2.calls == 0 and evaluator2.calls == 0  # blocked goals idle at 0 tokens
    assert engine.polls == 1                              # nothing left to poll
    assert len(notifier.sent) == sent_after_block         # no second ping


@pytest.mark.asyncio
async def test_lost_discovery_ref_blocks_and_notifies_owner(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "t_disc", "task", "analyze", is_discovery=True),
    ))
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = FakeEngine(poll_exc=GoalEngineError("unknown task_id: t_disc"))

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    st = store.load_status("g")
    assert st.phase == "blocked" and st.in_flight is None
    assert "task t_disc" in (st.blocked_on or "")
    # lifecycle pinned to executing: were it left "investigating", _classify
    # would route the NEXT tick back into INVESTIGATING and silently dispatch a
    # fresh review — contradicting the "paused; steer me" ping just sent.
    assert st.lifecycle == "executing"
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == 1 and "t_disc" in notifier.sent[0]

    # Next tick: a true block — idles at zero tokens, no re-dispatch, no re-ping.
    out2 = await _tick(store, "g", planner, evaluator, engine, notifier)
    assert out2 is Outcome.IDLE
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == 1
    assert store.load_status("g").phase == "blocked"


@pytest.mark.asyncio
async def test_lost_done_gate_ref_blocks_and_notifies_owner(tmp_path):
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "t_gate", "task", "verify", is_done_check=True),
    ))
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = FakeEngine(poll_exc=GoalEngineError("unknown task_id: t_gate"))

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    st = store.load_status("g")
    assert st.phase == "blocked" and st.in_flight is None
    assert "task t_gate" in (st.blocked_on or "")
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == 1 and "t_gate" in notifier.sent[0]


# ---- corrupt contract files block loudly (T0.4) -----------------------------


@pytest.mark.asyncio
async def test_corrupt_checklist_blocks_tick_loudly_then_idles(tmp_path):
    """A checklist.yaml that EXISTS but won't parse must BLOCK the goal with
    the real parse error — not silently read as "no checklist" and revert the
    goal to backlog mode. One OWNER ping, zero cognition; the next tick on the
    same corruption idles quietly (no wedge loop, no re-ping)."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")  # no STATUS yet → would plan+dispatch if healthy
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0 and evaluator.calls == 0  # corruption preempts cognition
    assert engine.dispatched == []
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert "checklist.yaml" in s.blocked_on  # blocked_on names the doc
    assert len(notifier.sent) == 1 and "corrupted" in notifier.sent[0]
    assert "checklist.yaml" in store.recent_log("g")

    # Tick again with the file still torn — idle, no re-ping, no log spam.
    out2 = await _tick(store, "g", planner, evaluator, engine, notifier)
    assert out2 is Outcome.IDLE
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_corrupt_firmed_draft_blocks_tick_via_load_effective_goal(tmp_path):
    """A torn firmed-draft.yaml used to make load_effective_goal silently
    return the BASE goal — dropping the firmed done_when / stub_acceptable /
    verify_cmd acceptance contract. Now it blocks at the tick's choke point
    with the same loud-once / idle-after shape as the checklist case."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    (tmp_path / "g" / "firmed-draft.yaml").write_text("status: [garbage\n")
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0 and evaluator.calls == 0
    assert engine.dispatched == []
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert "firmed-draft.yaml" in s.blocked_on
    assert len(notifier.sent) == 1 and "corrupted" in notifier.sent[0]

    out2 = await _tick(store, "g", planner, evaluator, engine, notifier)
    assert out2 is Outcome.IDLE
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_corrupt_doc_block_preserves_running_in_flight_ref(tmp_path):
    """Blocking on a corrupt contract file stops NEW cognition — it must not
    clobber the ref to an action that is already running. The ref survives the
    block so the action settles normally once the file is fixed."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="in_flight", in_flight=InFlight("devclaw", "start_program", "p1", "program")),
    )
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = FakeEngine(poll_result=PollResult(terminal=False, status="running"))

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert engine.polls == 0  # blocked before polling — no new work of any kind
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert s.in_flight is not None and s.in_flight.id == "p1"  # ref preserved


@pytest.mark.asyncio
async def test_missing_checklist_and_firmed_draft_stay_backlog_mode(tmp_path):
    """MISSING contract files remain the legitimate pre-decomposer /
    pre-firming state: the goal plans from the backlog (base goal), no block,
    no corruption noise."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")  # neither checklist.yaml nor firmed-draft.yaml
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED  # backlog mode unchanged
    assert planner.calls == 1
    assert store.load_status("g").phase == "in_flight"


# ---- blocked_kind: structured block classification (F8 prerequisite) --------
# A planned auto-heal pass must distinguish MECHANICAL blocks (condition
# cheaply re-checkable without an LLM) from NEEDS-ANSWER blocks (cognition
# asked the owner) and BUG blocks (the force_block escape hatch) — by a
# structured field, never by string-matching the blocked_on prose.


@pytest.mark.asyncio
async def test_blocked_kind_stamped_per_block_site(tmp_path):
    """Each block class stamps its machine-readable kind next to the prose:
    a torn checklist.yaml → mechanical:corrupt_doc, the dispatch-cap backstop
    → mechanical:dispatch_cap, a planner decision=blocked → needs_answer, and
    force_block (the illegal-transition escape hatch) → bug."""
    store = _store(tmp_path, Clock())
    planner, evaluator, engine, notifier = FakeClaude(ACT), FakeClaude(), FakeEngine(), RecordingNotifier()

    # mechanical:corrupt_doc — a contract file that exists but won't parse
    seed_goal(tmp_path, "gc")
    (tmp_path / "gc" / "checklist.yaml").write_text("not yaml: [garbage\n")
    assert await _tick(store, "gc", planner, evaluator, engine, notifier) is Outcome.BLOCKED
    s = store.load_status("gc")
    assert s.phase == "blocked" and s.blocked_kind == "mechanical:corrupt_doc"
    # the STATUS.md view surfaces the kind next to blocked_on (frontmatter + body)
    text = (tmp_path / "gc" / "STATUS.md").read_text()
    assert GoalStore._read_frontmatter(text)["blocked_kind"] == "mechanical:corrupt_doc"
    assert "blocked [mechanical:corrupt_doc] —" in text

    # mechanical:dispatch_cap — the runaway backstop (backlog 2 → cap 4)
    seed_goal(tmp_path, "gd")
    store.save_status("gd", GoalStatus(phase="idle", actions_dispatched=4))
    assert await _tick(store, "gd", planner, evaluator, engine, notifier) is Outcome.BLOCKED
    assert store.load_status("gd").blocked_kind == "mechanical:dispatch_cap"

    # needs_answer — the planner asked the owner a question
    seed_goal(tmp_path, "gq")
    ask = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    assert await _tick(store, "gq", ask, evaluator, engine, notifier) is Outcome.BLOCKED
    sq = store.load_status("gq")
    assert sq.blocked_on == "which auth provider?" and sq.blocked_kind == "needs_answer"

    # bug — the force_block illegal-transition escape hatch
    seed_goal(tmp_path, "gb")
    assert store.force_block("gb", "illegal state transition: EXEC_IDLE --ACHIEVE-> …") is True
    sb = store.load_status("gb")
    assert sb.phase == "blocked" and sb.blocked_kind == "bug"


def test_blocked_kind_cleared_on_unblock(tmp_path):
    """steer_goal lifts the block → blocked_kind returns to "" (enforced at
    the store's write choke point: any write landing on a non-blocked phase
    clears the kind, so no unblock path can leak a stale classification) and
    heal_attempts returns to 0 (a HUMAN lifting the block restores the full
    mechanical auto-heal budget)."""
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    goals_dir = tmp_path / "goals"
    seed_goal(goals_dir, "g")

    db = StateStore(str(tmp_path / "state.db"))
    try:
        cfg = GoalConfig(goals_dir=goals_dir, notify_url="", tick_seconds=900, eval_every=99, verify_done=False)
        svc = GoalService(TaskQueue(db), db, config=cfg)
        svc._goal_store.save_status(
            "g", GoalStatus(phase="blocked", blocked_on="cap hit",
                            blocked_kind="mechanical:dispatch_cap", actions_dispatched=5,
                            heal_attempts=2),
        )
        assert svc._goal_store.load_status("g").blocked_kind == "mechanical:dispatch_cap"

        svc.steer_goal("g", "resume with new approach")

        saved = svc._goal_store.load_status("g")
        assert saved.phase == "idle"
        assert saved.blocked_kind == ""
        assert saved.heal_attempts == 0
    finally:
        db.close()


# ---- corrupt-doc auto-heal (F8): mechanical recheck, damped, human-capped ----
# The tick's contract-file probe re-parses the docs every tick anyway — on a
# fixed doc that success IS the heal signal (zero LLM, zero subprocess). The
# persisted heal_attempts budget stops a FLAPPING file from turning the
# zero-token blocked steady-state into a plan + ping per cycle; only
# mechanical:corrupt_doc heals — needs_answer/bug/lost_ref/dispatch_cap stay
# human-gated.

#: a checklist.yaml body that parses cleanly (validate_checklist rejects an
#: empty list, so the minimal "fixed" doc carries one valid item).
GOOD_CHECKLIST = (
    "checklist:\n"
    "  - id: i-1\n"
    "    requirement: do the thing\n"
    "    evidence_target: tests pass\n"
)


@pytest.mark.asyncio
async def test_corrupt_doc_block_autoheals_when_doc_parses_again(tmp_path):
    """Torn checklist → BLOCKED (one ping, zero cognition). Fix the file → the
    very next tick auto-unblocks mechanically (log line, NO ping, heal 1/3)
    and proceeds to plan — the ensuing plan is the intended cost of a real
    heal. A later productive settle earns the heal budget back."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    planner, evaluator, engine, notifier = FakeClaude(ACT_FEATURE), FakeClaude(), FakeEngine(), RecordingNotifier()

    assert await _tick(store, "g", planner, evaluator, engine, notifier) is Outcome.BLOCKED
    assert await _tick(store, "g", planner, evaluator, engine, notifier) is Outcome.IDLE
    assert planner.calls == 0 and evaluator.calls == 0  # zero cognition while torn
    assert len(notifier.sent) == 1                       # the block ping only

    (tmp_path / "g" / "checklist.yaml").write_text(GOOD_CHECKLIST)
    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED                     # tick proceeded to plan + dispatch
    assert planner.calls == 1
    s = store.load_status("g")
    assert s.phase == "in_flight" and s.blocked_kind == ""
    assert s.heal_attempts == 1
    assert "auto-resumed: contract file parses again (heal 1/3)" in store.recent_log("g")
    assert len(notifier.sent) == 1                       # a heal logs; it never pings

    # A productive settle (same signal as the dispatch-cap refund) earns the
    # auto-heal budget back — the goal is demonstrably stable again.
    engine.poll_result = PollResult(terminal=True, status="done", detail="ok", gate_passed=True)
    await _tick(store, "g", planner, evaluator, engine, notifier)
    assert store.load_status("g").heal_attempts == 0


@pytest.mark.asyncio
async def test_corrupt_doc_flapping_capped_after_three_heals(tmp_path):
    """tear/fix ×3 heals fine; the 4th fix does NOT heal — the goal parks
    blocked with exactly one plain gave-up ping, and every further tick is
    zero-cognition idle. (tear/fix via the goal_docs row — after the first
    heal the checklist is DB-backed, the file is just a view.)"""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    sleeper = FakeClaude(json.dumps({"decision": "sleep", "note": "wait"}))
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()

    def tear():
        store._goal_state.write_doc("g", "checklist", "not yaml: [garbage", 1)

    def fix():
        store._goal_state.write_doc("g", "checklist", GOOD_CHECKLIST, 1)

    for n in (1, 2, 3):
        tear()
        assert await _tick(store, "g", sleeper, evaluator, engine, notifier) is Outcome.BLOCKED
        fix()
        assert await _tick(store, "g", sleeper, evaluator, engine, notifier) is Outcome.SLEPT
        assert store.load_status("g").heal_attempts == n
    assert sleeper.calls == 3  # exactly one plan per real heal

    # 4th flap: budget spent — the fix must NOT heal.
    tear()
    assert await _tick(store, "g", sleeper, evaluator, engine, notifier) is Outcome.BLOCKED
    fix()
    out = await _tick(store, "g", sleeper, evaluator, engine, notifier)

    assert out is Outcome.IDLE
    s = store.load_status("g")
    assert s.phase == "blocked" and s.blocked_kind == "mechanical:corrupt_doc"
    assert sleeper.calls == 3                                  # the gave-up tick spent zero cognition
    assert len([m for m in notifier.sent if "gave up" in m]) == 1

    # Parked: further ticks are zero-cognition, zero-ping idle.
    pings = len(notifier.sent)
    for _ in range(3):
        assert await _tick(store, "g", sleeper, evaluator, engine, notifier) is Outcome.IDLE
    assert sleeper.calls == 3 and evaluator.calls == 0
    assert len(notifier.sent) == pings                         # exactly one gave-up ping, ever
    assert store.load_status("g").phase == "blocked"


@pytest.mark.asyncio
async def test_autoheal_never_fires_on_needs_answer_or_bug_blocks(tmp_path):
    """A healthy store (the contract probe passes) must NOT unblock a
    needs_answer block (the owner has a question to answer) or a bug block
    (the force_block escape hatch) — auto-heal is corrupt_doc-only."""
    store = _store(tmp_path, Clock())
    evaluator, engine, notifier = FakeClaude(), FakeEngine(), RecordingNotifier()

    # needs_answer — driven by a real planner "blocked" decision.
    seed_goal(tmp_path, "gq")
    ask = FakeClaude(json.dumps({"decision": "blocked", "question": "which auth provider?"}))
    assert await _tick(store, "gq", ask, evaluator, engine, notifier) is Outcome.BLOCKED
    pings = len(notifier.sent)
    planner = FakeClaude(ACT)
    assert await _tick(store, "gq", planner, evaluator, engine, notifier) is Outcome.IDLE
    sq = store.load_status("gq")
    assert sq.phase == "blocked" and sq.blocked_kind == "needs_answer"
    assert sq.heal_attempts == 0                          # never even attempted
    assert planner.calls == 0 and len(notifier.sent) == pings

    # bug — the force_block illegal-transition escape hatch.
    seed_goal(tmp_path, "gb")
    assert store.force_block("gb", "illegal state transition: …") is True
    assert await _tick(store, "gb", planner, evaluator, engine, notifier) is Outcome.IDLE
    sb = store.load_status("gb")
    assert sb.phase == "blocked" and sb.blocked_kind == "bug"
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == pings                    # no heal chatter either


@pytest.mark.asyncio
async def test_lost_ref_block_stays_human_gated(tmp_path):
    """mechanical:lost_ref never auto-heals: _block_on_lost_ref destroys the
    in_flight ref at block time (the id survives only in blocked_on prose), so
    there is nothing mechanical left to recheck — the block is deliberately
    human-gated even though the store itself is perfectly healthy."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t_gone", "task", "add /health"),
    ))
    planner, evaluator, notifier = FakeClaude(ACT), FakeClaude(), RecordingNotifier()
    engine = FakeEngine(poll_exc=GoalEngineError("unknown task_id: t_gone"))

    assert await _tick(store, "g", planner, evaluator, engine, notifier) is Outcome.BLOCKED
    assert store.load_status("g").blocked_kind == "mechanical:lost_ref"
    pings = len(notifier.sent)

    # Healthy store, many ticks: stays blocked, zero cognition, no auto-unblock.
    for _ in range(3):
        assert await _tick(store, "g", planner, evaluator, engine, notifier) is Outcome.IDLE
    s = store.load_status("g")
    assert s.phase == "blocked" and s.blocked_kind == "mechanical:lost_ref"
    assert s.heal_attempts == 0
    assert planner.calls == 0 and evaluator.calls == 0
    assert len(notifier.sent) == pings


# ---- prep-failure auto-heal (F8): ls-remote recheck on a capped backoff -----
# Unlike the corrupt-doc probe (free, every tick), the prep recheck costs a
# git subprocess, so it runs only when the persisted next_heal_at window is
# open — between windows a blocked goal is a zero-subprocess, zero-cognition
# tick. The ls-remote seam (tick_guards._ls_remote_ok_sync) is stubbed here;
# the block itself is driven by the REAL prep-failure path.


@pytest.mark.asyncio
async def test_prep_block_autoheals_when_remote_reachable_again(tmp_path, monkeypatch):
    """Prep-blocked goal + reachable remote → the first due recheck heals
    (one ls-remote against the goal's repo_url, no ping, log line), and the
    tick proceeds to plan + dispatch with the REAL prepare_ws."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    # investigation path: prep runs BEFORE any cognition, so the block tick
    # is zero-token (mirrors test_investigation_prep_failure_blocks_without_cognition)
    store.save_status("g", GoalStatus(phase="idle", lifecycle="investigating"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()

    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.BLOCKED
    assert store.load_status("g").blocked_kind == "mechanical:prep"
    assert planner.calls == 0 and len(notifier.sent) == 1

    probes: list[str] = []

    def reachable(url: str) -> bool:
        probes.append(url)
        return True

    monkeypatch.setattr("devclaw.goal.tick_guards._ls_remote_ok_sync", reachable)
    out = await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=fake_prepare)

    assert out is Outcome.DISPATCHED                      # healed and went on to plan
    assert probes == ["https://example.com/demo.git"]     # exactly one ls-remote, the goal's URL
    assert planner.calls == 1
    s = store.load_status("g")
    assert s.phase == "in_flight" and s.blocked_kind == ""
    assert s.heal_attempts == 1 and s.next_heal_at is None
    assert "auto-resumed: repo reachable again (heal 1/5)" in store.recent_log("g")
    assert len(notifier.sent) == 1                        # a heal logs; it never pings


@pytest.mark.asyncio
async def test_prep_heal_respects_backoff_window(tmp_path, monkeypatch):
    """A failed recheck arms an exponential next_heal_at window (30min·2^n,
    capped): before it opens, a tick runs NO recheck at all — zero subprocess,
    zero cognition. The heal budget continues across windows."""
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="investigating"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()
    await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare)

    probes: list[str] = []
    reachable = {"now": False}

    def recheck(url: str) -> bool:
        probes.append(url)
        return reachable["now"]

    monkeypatch.setattr("devclaw.goal.tick_guards._ls_remote_ok_sync", recheck)

    # First recheck is due immediately (no window yet) — fails, arms 30min.
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    s = store.load_status("g")
    assert len(probes) == 1 and s.heal_attempts == 1 and s.next_heal_at is not None

    # Inside the window: NO recheck — the tick never even spawns the subprocess.
    clock.advance(10 * 60)
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert len(probes) == 1

    # Window open (t=31min > 30min): recheck fires, fails, window doubles to 1h.
    clock.advance(21 * 60)
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert len(probes) == 2 and store.load_status("g").heal_attempts == 2

    # 31min into the 1h window: still closed.
    clock.advance(31 * 60)
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert len(probes) == 2
    assert planner.calls == 0                             # zero cognition this whole time

    # Past the window and the remote is back: heal, budget continuing at 3/5.
    clock.advance(30 * 60)
    reachable["now"] = True
    out = await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=fake_prepare)
    assert out is Outcome.DISPATCHED
    assert len(probes) == 3
    s = store.load_status("g")
    assert s.blocked_kind == "" and s.heal_attempts == 3 and s.next_heal_at is None


@pytest.mark.asyncio
async def test_prep_heal_gives_up_after_cap(tmp_path, monkeypatch):
    """5 failed rechecks spend the budget; the next tick parks the goal with
    exactly ONE plain gave-up ping and never rechecks again — a parked goal
    is a zero-subprocess, zero-cognition tick until a human steers."""
    clock = Clock()
    store = _store(tmp_path, clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="investigating"))
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()
    await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare)

    probes: list[str] = []

    def never_reachable(url: str) -> bool:
        probes.append(url)
        return False

    monkeypatch.setattr("devclaw.goal.tick_guards._ls_remote_ok_sync", never_reachable)

    # 5 failed rechecks, jumping past every backoff window (max 6h).
    for n in (1, 2, 3, 4, 5):
        clock.advance(7 * 3600)
        assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
        assert store.load_status("g").heal_attempts == n
    assert len(probes) == 5
    assert not any("gave up" in m for m in notifier.sent)

    # Budget spent: the next tick parks — one plain gave-up ping, NO recheck.
    clock.advance(7 * 3600)
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert len(probes) == 5                                # parked before any subprocess
    assert len([m for m in notifier.sent if "gave up" in m]) == 1
    s = store.load_status("g")
    assert s.phase == "blocked" and s.blocked_kind == "mechanical:prep"

    # And stays parked: no rechecks, no pings, zero cognition.
    pings = len(notifier.sent)
    for _ in range(3):
        clock.advance(7 * 3600)
        assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert len(probes) == 5 and len(notifier.sent) == pings
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_prep_heal_checks_workspace_git_when_no_repo_url(tmp_path, monkeypatch):
    """A goal with no repo_url (pre-existing-workspace config) rechecks by
    stat — does <workspace_dir>/.git exist — and must never spawn ls-remote."""
    clock = Clock()
    store = _store(tmp_path, clock)
    ws = tmp_path / "ws"
    seed_goal(tmp_path, "g", repo_url=None, workspace_dir=str(ws))
    store.save_status("g", GoalStatus(phase="idle"))  # executing path (world-research skips prep)
    planner, engine, notifier = FakeClaude(ACT), FakeEngine(), RecordingNotifier()

    def no_ls_remote(url: str) -> bool:
        raise AssertionError("ls-remote must not run for a goal without a repo_url")

    monkeypatch.setattr("devclaw.goal.tick_guards._ls_remote_ok_sync", no_ls_remote)

    # Executing path: plan → dispatch → prep fails → block (one plan call).
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.BLOCKED
    assert store.load_status("g").blocked_kind == "mechanical:prep"

    # Workspace still isn't a checkout → the stat recheck fails, backoff arms.
    assert await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=_failing_prepare) is Outcome.IDLE
    assert store.load_status("g").heal_attempts == 1

    # The checkout appears; past the window the recheck passes and heals.
    (ws / ".git").mkdir(parents=True)
    clock.advance(31 * 60)
    out = await _tick_prep(store, "g", planner, engine, notifier, prepare_ws=fake_prepare)
    assert out is Outcome.DISPATCHED
    s = store.load_status("g")
    assert s.blocked_kind == "" and s.heal_attempts == 2 and s.next_heal_at is None
