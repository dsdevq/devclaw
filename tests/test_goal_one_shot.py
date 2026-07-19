"""One-shot goal mode (ADR 0003 stage 2) — the LIVE tick path.

The dial: a ``mode: one_shot`` goal runs ZERO per-tick planner cognition.
Its checklist (the decomposer's output) IS the plan — the executing phase
dispatches every pending item as ONE planned program (parallel DAG in the
queue), per-item verdicts come back via each child task's ``plan_key``, and
when the checklist drains the goal proposes done MECHANICALLY — still gated
on the grounded done-gate review + evaluator, same as long-lived mode.

Every test here asserts ``planner.calls == 0``: that IS the mode's contract.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal.models import Checklist, ChecklistItem, GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.goal.tick_settle import _settle_program_items
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


def _checklist() -> Checklist:
    return Checklist(items=[
        ChecklistItem(
            id="scaffold", requirement="Create the csproj.",
            evidence_target="backend/src/Foo.csproj", scaffold=True,
        ),
        ChecklistItem(
            id="wire-x", requirement="Wire the X tool.",
            evidence_target="backend/src/Tools/X.cs", depends_on=["scaffold"],
        ),
    ])


async def _tick(store, gid, planner, evaluator, engine, notifier):
    return await tick_goal(
        gid, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, prepare_ws=fake_prepare,
    )


# ---- mode round-trip -------------------------------------------------------


def test_goal_mode_roundtrips_via_goal_yaml(tmp_path):
    store = _store(tmp_path)
    g = store.create_goal(
        "os", objective="ship it", workspace_dir="/repos/x", mode="one_shot",
    )
    assert g.mode == "one_shot"
    assert store.load_goal("os").mode == "one_shot"


def test_legacy_goal_yaml_without_mode_loads_long_lived(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # writes no mode field — a pre-ADR-0003 file
    assert store.load_goal("g").mode == "long_lived"


# ---- executing: dispatch the whole checklist as ONE planned program --------


@pytest.mark.asyncio
async def test_one_shot_dispatches_whole_checklist_as_one_planned_program_zero_planner_calls(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", _checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    planner = FakeClaude(role="planner")     # must NEVER be called
    engine = FakeEngine(dispatch_ref=InFlight(
        "devclaw", "start_program", "p1", "program", "one-shot batch",
    ))
    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), engine, RecordingNotifier())

    assert out is Outcome.DISPATCHED
    assert planner.calls == 0                # the mode's whole point
    action, _goal, _nu = engine.dispatched[0]
    assert action.tool == "start_program"
    assert action.addresses == ["scaffold", "wire-x"]
    # the plan rode the action — the queue must NOT re-plan
    assert [p.key for p in action.planned] == ["scaffold", "wire-x"]  # topo order
    assert action.planned[0].scaffold is True
    # dispatch hook flipped the items so nothing re-picks them
    cl = store.read_checklist("g")
    assert {i.status for i in cl.items} == {"in_flight"}
    assert store.load_status("g").phase == "in_flight"


@pytest.mark.asyncio
async def test_one_shot_redispatches_only_the_remainder_after_partial_failure(tmp_path):
    """Program settled with one child done, one failed: the succeeded item is
    done with grounded evidence, the failed one returns to the pool with its
    attempt counted — and the SAME tick chains into a smaller program carrying
    only the remainder. Zero planner calls throughout."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="scaffold", requirement="r", evidence_target="e", status="in_flight"),
        ChecklistItem(id="wire-x", requirement="r2", evidence_target="e2", status="in_flight"),
    ]))
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "start_program", "p1", "program", "one-shot batch",
                           addresses=["scaffold", "wire-x"]),
    ))
    planner = FakeClaude(role="planner")
    engine = FakeEngine(
        poll_result=PollResult(
            terminal=True, status="failed", detail="one child failed",
            pr_url="https://x/pr/1",
            gate_passed=None,
            tasks=[
                {"plan_key": "scaffold", "status": "done", "gate_passed": True,
                 "pr_url": "https://x/pr/1", "error": None},
                {"plan_key": "wire-x", "status": "failed", "gate_passed": False,
                 "pr_url": None, "error": "build broke"},
            ],
        ),
        dispatch_ref=InFlight("devclaw", "start_program", "p2", "program", "one-shot batch 2"),
    )
    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), engine, RecordingNotifier())

    assert out is Outcome.DISPATCHED
    assert planner.calls == 0
    cl = store.read_checklist("g")
    scaffold = next(i for i in cl.items if i.id == "scaffold")
    wire = next(i for i in cl.items if i.id == "wire-x")
    # scaffold settled done (its own child's verdict) and must NOT ride the
    # follow-up program:
    assert scaffold.status == "done"
    assert "https://x/pr/1" in (scaffold.evidence or "")
    action, _g, _n = engine.dispatched[-1]
    assert action.addresses == ["wire-x"]
    assert [p.key for p in action.planned] == ["wire-x"]
    assert wire.status == "in_flight"        # re-dispatched remainder
    assert wire.attempts == 1                # the failure was counted


@pytest.mark.asyncio
async def test_one_shot_drained_checklist_opens_done_gate_mechanically(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e",
                      status="done", evidence="PR x (unmerged) · sandbox gate=passed"),
    ]))
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    planner = FakeClaude(role="planner")
    engine = FakeEngine(dispatch_ref=InFlight(
        "devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True,
    ))
    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), engine, RecordingNotifier())

    assert out is Outcome.VERIFYING
    assert planner.calls == 0                # done proposal is mechanical
    action, _g, _n = engine.dispatched[0]
    assert action.tool == "review_repository"
    s = store.load_status("g")
    assert s.phase == "verifying"
    assert s.in_flight is not None and s.in_flight.is_done_check is True


@pytest.mark.asyncio
async def test_one_shot_done_gate_not_achieved_blocks_for_owner_instead_of_looping(tmp_path):
    """The single pass is spent: a not-achieved review must PARK the goal
    (needs_answer), never RESUME_IDLE — idle would re-drain → re-review every
    tick, burning a sandbox review per heartbeat forever."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e", status="done", evidence="x"),
    ]))
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify", is_done_check=True),
    ))
    planner = FakeClaude(role="planner")
    evaluator = FakeClaude(json.dumps({
        "verdict": "off_track", "rationale": "the endpoint is not tested",
        "corrections": ["add a test"],
    }))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="no test found"))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0
    s = store.load_status("g")
    assert s.phase == "blocked"
    assert s.blocked_kind == "needs_answer"
    assert "done_when is not confirmed" in (s.blocked_on or "")
    assert any("done_when is not confirmed" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_one_shot_without_checklist_blocks_loudly(tmp_path):
    """A one-shot goal with no plan can never progress (there is no backlog
    fallback in this mode) — fail loud, not idle-forever."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")   # no checklist written
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    planner = FakeClaude(role="planner")
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), FakeEngine(), notifier)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0
    s = store.load_status("g")
    assert s.phase == "blocked" and s.blocked_kind == "bug"
    assert any("no checklist" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_one_shot_blocked_tick_costs_zero(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", _checklist())
    store.save_status("g", GoalStatus(
        phase="blocked", lifecycle="executing", blocked_on="q", blocked_kind="needs_answer",
    ))
    planner = FakeClaude(role="planner")
    evaluator = FakeClaude(role="evaluator")

    out = await _tick(store, "g", planner, evaluator, FakeEngine(), RecordingNotifier())

    assert out is Outcome.IDLE
    assert planner.calls == 0 and evaluator.calls == 0   # the sacred guard


# ---- _settle_program_items (pure) ------------------------------------------


def test_settle_program_items_grades_each_item_by_its_own_child():
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e", status="in_flight"),
        ChecklistItem(id="b", requirement="r", evidence_target="e", status="in_flight"),
    ])
    poll = PollResult(
        terminal=True, status="failed", detail="program failed",
        tasks=[
            {"plan_key": "a", "status": "done", "gate_passed": True, "pr_url": "https://x/1", "error": None},
            {"plan_key": "b", "status": "failed", "gate_passed": None, "pr_url": None, "error": "boom"},
        ],
    )
    updated = _settle_program_items(cl, ["a", "b"], poll)
    a = next(i for i in updated.items if i.id == "a")
    b = next(i for i in updated.items if i.id == "b")
    assert a.status == "done" and "https://x/1" in (a.evidence or "")
    assert b.status == "not_started" and b.attempts == 1
    assert any("boom" in note for note in b.failure_log)


def test_settle_program_items_missing_child_falls_back_to_aggregate():
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e", status="in_flight"),
    ])
    poll = PollResult(terminal=True, status="done", detail="", pr_url="https://x/1",
                      gate_passed=None, tasks=[{"plan_key": None, "status": "done"}])
    updated = _settle_program_items(cl, ["a"], poll)
    assert updated.items[0].status == "done"   # aggregate 'done' verdict applied


# ---- guard findings (F1-F3) -------------------------------------------------


@pytest.mark.asyncio
async def test_long_lived_program_settle_stays_aggregate_even_with_child_breakdown(tmp_path):
    """F1: per-child grading is scoped to one_shot. A LONG-LIVED goal's
    program children are planned by the queue's decomposer, whose slug keys
    can accidentally collide with checklist item ids — an accidental plan_key
    match must NOT flip a milestone item to done off a partially-failed
    program; the aggregate verdict stays authoritative."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # long_lived (default)
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="scaffold", requirement="r", evidence_target="e", status="in_flight"),
    ]))
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "start_program", "p1", "program", "prog",
                           addresses=["scaffold"]),
    ))
    planner = FakeClaude(json.dumps({"decision": "sleep", "note": "ok"}), role="planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="failed", detail="program failed",
        tasks=[{"plan_key": "scaffold", "status": "done", "gate_passed": True,
                "pr_url": "https://x/1", "error": None}],
    ))
    await _tick(store, "g", planner, FakeClaude(role="evaluator"), engine, RecordingNotifier())

    item = store.read_checklist("g").items[0]
    assert item.status == "not_started"      # aggregate FAILED verdict applied
    assert item.attempts == 1


@pytest.mark.asyncio
async def test_one_shot_oversized_checklist_blocks_loudly_instead_of_error_looping(tmp_path):
    """F2: the MAX_PROGRAM_TASKS brake raising out of the one-shot handler
    would reproduce identically every heartbeat (Outcome.ERROR forever, no
    ping). It must park the goal with an actionable owner message instead."""
    from devclaw.planner import MAX_PROGRAM_TASKS

    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id=f"t{i}", requirement="r", evidence_target="e")
        for i in range(MAX_PROGRAM_TASKS + 1)
    ]))
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    planner = FakeClaude(role="planner")
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), FakeEngine(), notifier)

    assert out is Outcome.BLOCKED
    assert planner.calls == 0
    s = store.load_status("g")
    assert s.phase == "blocked" and s.blocked_kind == "needs_answer"
    assert "brake" in (s.blocked_on or "")
    assert any("plan rejected" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_one_shot_never_dispatches_dependents_of_a_breaker_blocked_item(tmp_path):
    """F3: same contract as checklist.ready_items — work whose prerequisite is
    known-failed must not dispatch. Reachable via breaker-park → resume_goal
    (which does not reset the tripped item): the remainder program must hold
    the dependent chain, not burn attempts on doomed work."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", mode="one_shot")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="base", requirement="r", evidence_target="e", status="blocked",
                      evidence="circuit breaker: 3 straight failed attempts"),
        ChecklistItem(id="mid", requirement="r", evidence_target="e", depends_on=["base"]),
        ChecklistItem(id="leaf", requirement="r", evidence_target="e", depends_on=["mid"]),
        ChecklistItem(id="free", requirement="r", evidence_target="e"),
    ]))
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    planner = FakeClaude(role="planner")
    engine = FakeEngine(dispatch_ref=InFlight(
        "devclaw", "start_program", "p1", "program", "one-shot batch",
    ))
    out = await _tick(store, "g", planner, FakeClaude(role="evaluator"), engine, RecordingNotifier())

    assert out is Outcome.DISPATCHED
    action, _g, _n = engine.dispatched[0]
    # only the unaffected item rides; the blocked chain (mid, leaf) is held
    assert action.addresses == ["free"]
    assert [p.key for p in action.planned] == ["free"]
