"""Tranche 1/PR7 — transactional dispatch + settle, goal_settlements live.

Closes the two remaining crash/race windows the goal heartbeat had:
1. dispatch — task/program row creation + the DISPATCH transition + the log
   row now commit (or roll back) as ONE unit.
2. settle — the settlement row + delivery row + log row + checklist update
   + the *_SETTLED transition now commit (or roll back) as ONE unit; merge/
   reconcile/mirror-renders happen strictly AFTER commit.

Also pins the mirror-discipline mechanism (file writes deferred while a
transaction() is open, flushed/discarded by the caller), the lazy
goal_settlements seed from historical log rows, and the startup orphan
sweep (extended from programs-only to tasks, demoted from per-tick to
once-per-service-start).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from devclaw.goal.engine import InProcessEngine
from devclaw.goal.models import Checklist, ChecklistItem, GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import (
    Outcome,
    TickContext,
    _resolve_polling_action,
    _run_atomic,
    sweep_orphaned_refs,
    tick_goal,
)
from devclaw.goal.transitions import TransitionConflict
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

ACT_FEATURE = json.dumps({
    "decision": "act", "note": "ship it",
    "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": False}],
})
SLEEP = json.dumps({"decision": "sleep", "note": "ok"})


async def _ok_runner(request) -> dict:
    return {"status": "ok", "message": f"did: {request.goal[:40]}"}


def _wired(tmp_path, clock=None):
    """Production wiring — one shared StateStore backs BOTH the task queue
    and the goal store, the shape the atomic-dispatch tests need: a fake,
    isolated GoalStore can't observe whether a real task row survived a
    rollback, only the shared-store wiring can."""
    state = StateStore(str(tmp_path / "state.db"))
    queue = TaskQueue(state, runner=_ok_runner)
    engine = InProcessEngine(queue, state)
    goals_dir = tmp_path / "goals"
    goal_store = GoalStore(goals_dir, now=clock or Clock(), state=state)
    return state, queue, engine, goal_store, goals_dir


class _ConflictInjectingEngine(InProcessEngine):
    """Wraps the REAL InProcessEngine: after actually dispatching (creating
    the task/program row — proving the row-creation write DOES join the
    atomic unit), bumps the goal's status version via a second write through
    the SAME GoalStore — models a concurrent writer (steer_goal, a parallel
    tick) landing between the caller's load and its own transition() call.
    Same "mid-await" trick test_goal_transitions.py uses (a callback writes
    to the store as a side effect of being invoked, simulating a second
    writer without literal thread concurrency); applied here inside
    engine.dispatch() since the atomic dispatch unit has no cognition await
    to land inside — the version bump has to come from dispatch itself.
    update_status_fields() is used (not append_steering/append_log) so the
    ONLY side effect is a goal_status.version bump — no incidental file
    writes to confound a byte-compare assertion elsewhere."""

    def __init__(self, queue, store, goal_store, goal_id):
        super().__init__(queue, store)
        self._goal_store = goal_store
        self._goal_id = goal_id

    async def dispatch(self, action, goal, notify_url):
        ref = await super().dispatch(action, goal, notify_url)
        self._goal_store.update_status_fields(self._goal_id, last_tick_at="conflict-injected")
        return ref


class _ConflictInjectingFakeEngine(FakeEngine):
    """Same trick as :class:`_ConflictInjectingEngine`, over the lightweight
    FakeEngine double (no real task/program row) for tests that only need
    an isolated GoalStore, not the shared-store production wiring."""

    def __init__(self, goal_store, goal_id, **kw):
        super().__init__(**kw)
        self._goal_store = goal_store
        self._goal_id = goal_id

    async def dispatch(self, action, goal, notify_url):
        ref = await super().dispatch(action, goal, notify_url)
        self._goal_store.update_status_fields(self._goal_id, last_tick_at="conflict-injected")
        return ref


class _MidPollConflictEngine(FakeEngine):
    """FakeEngine whose poll() bumps the goal's status version as a side
    effect before returning — the settle-side counterpart of
    _ConflictInjectingFakeEngine, forcing a CAS conflict inside the atomic
    settle transaction (poll() runs OUTSIDE that transaction, so the bump
    lands before the transaction even opens, same net effect)."""

    def __init__(self, goal_store, goal_id, **kw):
        super().__init__(**kw)
        self._goal_store = goal_store
        self._goal_id = goal_id

    async def poll(self, ref):
        result = await super().poll(ref)
        self._goal_store.update_status_fields(self._goal_id, last_tick_at="conflict-injected")
        return result


class _ProgramFinderEngine(FakeEngine):
    """FakeEngine + the latest_program_for_goal finder the sweep probes."""

    def __init__(self, *, program: "tuple[str, str] | None" = None, **kw):
        super().__init__(**kw)
        self.program = program

    def latest_program_for_goal(self, goal_id: str):
        return self.program


class _TaskFinderEngine(FakeEngine):
    """FakeEngine + the latest_task_for_goal finder the sweep probes (PR7 —
    extends re-adoption from programs-only to tasks)."""

    def __init__(self, *, task: "tuple[str, str, str] | None" = None, **kw):
        super().__init__(**kw)
        self.task = task

    def latest_task_for_goal(self, goal_id: str):
        return self.task


# ---- 1. crash-mid-dispatch leaves nothing ----------------------------------


@pytest.mark.asyncio
async def test_crash_mid_dispatch_leaves_nothing(tmp_path):
    """A CAS conflict landing INSIDE the atomic dispatch transaction (task
    row creation + the DISPATCH transition + the log row) rolls the WHOLE
    unit back — no task row survives in the queue's own store, no in_flight
    ref, and the goal replans on its next tick instead of orphaning a
    dispatched-but-unrecorded task."""
    state, queue, _engine, goal_store, goals_dir = _wired(tmp_path)
    seed_goal(goals_dir, "g", cadence="1d")
    goal_store.save_status(
        "g", GoalStatus(phase="idle", lifecycle="executing"),
    )

    engine = _ConflictInjectingEngine(queue, state, goal_store, "g")

    out = await tick_goal(
        "g", store=goal_store, engine=engine,
        planner_caller=FakeClaude(ACT_FEATURE), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out is Outcome.CONFLICT
    # No task row survives the rollback — the FIRST write inside the atomic
    # unit (engine.dispatch's create_task) rolled back with everything else.
    assert state.list_tasks(parent_goal_id="g") == []
    s = goal_store.load_status("g")
    assert s.in_flight is None
    assert s.phase == "idle"


# ---- 2. crash-mid-settle is idempotent -------------------------------------


@pytest.mark.asyncio
async def test_crash_mid_settle_is_idempotent(tmp_path):
    """A CAS conflict landing INSIDE the atomic settle transaction rolls the
    settlement row, the delivery row, the log row, AND the transition back
    together — no partial artifacts. The retry tick then re-polls the SAME
    terminal ref and settles cleanly: exactly ONE delivery section, ONE
    settle log line, ONE settlement row — no duplicates from the aborted
    attempt."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "add /health")
    store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing", in_flight=ref))

    poll_result = PollResult(
        terminal=True, status="done", detail="did it", pr_url="https://x/pr/9", gate_passed=True,
    )
    conflicting_engine = _MidPollConflictEngine(store, "g", poll_result=poll_result)

    out = await tick_goal(
        "g", store=store, engine=conflicting_engine,
        planner_caller=FakeClaude(SLEEP), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out is Outcome.CONFLICT
    s = store.load_status("g")
    assert s.in_flight is not None and s.in_flight.id == "t1"  # preserved, not cleared
    assert store.is_settled("g", "t1") is False  # no settlement row
    assert store.recent_deliveries("g") == ""  # no delivery row
    log_after_conflict = store.recent_log("g")
    assert "implement_feature t1 → done" not in log_after_conflict  # no settle log line
    assert "tick abandoned" in log_after_conflict  # tick_goal's OWN, separate write

    # Retry: same terminal ref, no conflict injection this time.
    clean_engine = FakeEngine(poll_result=poll_result)
    out2 = await tick_goal(
        "g", store=store, engine=clean_engine,
        planner_caller=FakeClaude(SLEEP), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out2 not in (Outcome.CONFLICT, Outcome.ERROR)
    assert store.is_settled("g", "t1") is True
    log_final = store.recent_log("g")
    assert log_final.count("implement_feature t1 → done") == 1  # exactly ONE
    deliveries = store.recent_deliveries("g")
    assert deliveries.count("## [") == 1  # exactly ONE delivery section


# ---- 3. dispatch defers the pump -------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_defers_the_pump(tmp_path):
    """submit(pump=False) creates the row WITHOUT claiming/launching it — a
    later explicit pump()/kick() call is what actually starts it. This is
    the mechanism the goal tick's atomic dispatch transaction depends on: if
    engine.dispatch() pumped synchronously, it would claim + spawn
    background execution for OTHER, unrelated pending rows as part of the
    SAME atomic unit — irreversible if that unit later rolls back."""
    launched: list[str] = []

    async def _rec_runner(request) -> dict:
        launched.append(request.goal)
        return {"status": "ok"}

    state = StateStore(str(tmp_path / "state.db"))
    queue = TaskQueue(state, runner=_rec_runner)

    task_id = queue.submit(kind="implement_feature", workspace_dir="/ws", goal="g", pump=False)
    await queue.drain()

    assert launched == []  # not claimed/launched yet
    assert state.get_task(task_id).status == "pending"

    queue.pump()  # the explicit post-commit kick production code performs
    await queue.drain()

    assert launched == ["g"]
    assert state.get_task(task_id).status == "done"


@pytest.mark.asyncio
async def test_dispatch_error_leaves_no_task_row(tmp_path):
    """engine.dispatch() raising INSIDE the atomic dispatch transaction
    rolls back cleanly — no task row survives — and the goal follows the
    pre-PR7 error path (a fresh, separate RESUME_IDLE write) OUTSIDE the
    aborted unit."""
    state, queue, _engine, goal_store, goals_dir = _wired(tmp_path)
    seed_goal(goals_dir, "g")
    goal_store.save_status(
        "g", GoalStatus(phase="idle", lifecycle="executing"),
    )

    class _RaisingEngine(InProcessEngine):
        async def dispatch(self, action, goal, notify_url):
            raise RuntimeError("dispatch exploded")

    out = await tick_goal(
        "g", store=goal_store, engine=_RaisingEngine(queue, state),
        planner_caller=FakeClaude(ACT_FEATURE), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out is Outcome.ERROR
    assert state.list_tasks(parent_goal_id="g") == []
    s = goal_store.load_status("g")
    assert s.phase == "idle"
    assert s.in_flight is None


# ---- 4. no phantom flag_items ----------------------------------------------


@pytest.mark.asyncio
async def test_no_phantom_flag_items_on_aborted_dispatch(tmp_path):
    """A checklist item the dispatch hook would flag in_flight must NOT
    survive a rolled-back dispatch transaction. Pre-PR7 this write was NOT
    part of the dispatch's atomic unit and COULD survive a rollback
    (a small honesty gap PR7 closes by moving _flag_items_in_flight inside
    the transaction)."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="scaffold", requirement="do it", evidence_target="x"),
    ]))
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    act_with_addresses = json.dumps({
        "decision": "act", "note": "scaffold",
        "actions": [{
            "tool": "implement_feature", "goal": "do it", "open_pr": True, "addresses": ["scaffold"],
        }],
    })
    engine = _ConflictInjectingFakeEngine(store, "g")

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(act_with_addresses), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out is Outcome.CONFLICT
    cl = store.read_checklist("g")
    assert cl.items[0].status == "not_started"  # NOT left in_flight


# ---- 5. mirror discipline ---------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_discipline_aborted_settle_leaves_files_untouched(tmp_path):
    """An aborted settle transaction must leave log.md / deliveries.md /
    checklist.yaml byte-identical to before — a rollback must never leave a
    file mirroring state the DB no longer has. Exercises the settle
    resolver directly (rather than through tick_goal) so the assertion is a
    true byte-compare, unconfounded by tick_goal's OWN separate "tick
    abandoned" log line on TransitionConflict."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="scaffold", requirement="do it", evidence_target="x", status="in_flight"),
    ]))
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it", addresses=["scaffold"])
    store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing", in_flight=ref))
    store.append_log("g", "seed line")  # log.md exists with known content

    log_before = (tmp_path / "g" / "log.md").read_text()
    checklist_before = (tmp_path / "g" / "checklist.yaml").read_text()
    deliveries_path = tmp_path / "g" / "deliveries.md"
    deliveries_existed_before = deliveries_path.exists()
    deliveries_before = deliveries_path.read_text() if deliveries_existed_before else None

    poll_result = PollResult(
        terminal=True, status="done", detail="did it", pr_url="https://x/pr/1", gate_passed=True,
    )
    engine = _MidPollConflictEngine(store, "g", poll_result=poll_result)
    goal = store.load_goal("g")
    status = store.load_status("g")
    ctx = TickContext(
        store=store, engine=engine,
        planner_caller=FakeClaude(), evaluator_caller=FakeClaude(), notifier=RecordingNotifier(),
    )

    with pytest.raises(TransitionConflict):
        await _resolve_polling_action("g", goal, status, ctx)

    assert (tmp_path / "g" / "log.md").read_text() == log_before
    assert (tmp_path / "g" / "checklist.yaml").read_text() == checklist_before
    assert deliveries_path.exists() == deliveries_existed_before
    if deliveries_existed_before:
        assert deliveries_path.read_text() == deliveries_before


@pytest.mark.asyncio
async def test_mirror_discipline_successful_settle_matches_rows(tmp_path):
    """The counterpart of the aborted case above: a successful settle's
    mirrors (log.md / deliveries.md / checklist.yaml) match what actually
    landed in the rows."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.write_checklist("g", Checklist(items=[
        ChecklistItem(id="scaffold", requirement="do it", evidence_target="x", status="in_flight"),
    ]))
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it", addresses=["scaffold"])
    store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing", in_flight=ref))

    poll_result = PollResult(
        terminal=True, status="done", detail="did it", pr_url="https://x/pr/1", gate_passed=True,
    )
    engine = FakeEngine(poll_result=poll_result)
    goal = store.load_goal("g")
    status = store.load_status("g")
    ctx = TickContext(
        store=store, engine=engine,
        planner_caller=FakeClaude(), evaluator_caller=FakeClaude(), notifier=RecordingNotifier(),
    )

    await _resolve_polling_action("g", goal, status, ctx)

    assert "implement_feature t1 → done" in store.recent_log("g")
    assert "implement_feature t1 → done" in (tmp_path / "g" / "log.md").read_text()
    assert "did it" in store.recent_deliveries("g")
    assert "did it" in (tmp_path / "g" / "deliveries.md").read_text()
    cl_rows = store.read_checklist("g")
    assert cl_rows.items[0].status == "done"
    assert "status: done" in (tmp_path / "g" / "checklist.yaml").read_text()


# ---- 6. settlement seeding ---------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_seeding_from_legacy_log(tmp_path):
    """A legacy goal with a pre-PR7 log line and zero settlement rows must
    answer is_settled(...) True from the seed alone (no re-adoption
    needed) — matching exactly what the old log_contains(f" {id} → ")
    guard used to answer True for. A DIFFERENT, unlogged ref on the SAME
    goal is still re-adopted normally."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    d = tmp_path / "g"
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.md").write_text(
        "# g — log\n\n- [2026-07-01T00:00:00] start_program p-1 → done\n"
    )
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    assert store.is_settled("g", "p-1") is True  # seeded from the legacy line

    swept = await sweep_orphaned_refs(store, _ProgramFinderEngine(program=("p-1", "some program")))
    assert swept == {}  # already settled — sweep leaves it alone

    swept2 = await sweep_orphaned_refs(store, _ProgramFinderEngine(program=("p-2", "unlogged program")))
    assert swept2 == {"g": "program p-2"}  # a different, unlogged program IS re-adopted


# ---- 7. sweep extends to tasks ----------------------------------------------


@pytest.mark.asyncio
async def test_sweep_extends_to_tasks(tmp_path):
    """The startup sweep re-adopts a lost TASK ref, not just programs — the
    PR7 extension. Re-adopted as a plain action ref (no is_done_check/
    is_discovery — those flags lived only on the lost ref)."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    engine = _TaskFinderEngine(task=("t-lost", "add /health", "implement_feature"))
    swept = await sweep_orphaned_refs(store, engine)

    assert swept == {"g": "task t-lost"}
    s = store.load_status("g")
    assert s.in_flight is not None
    assert s.in_flight.id == "t-lost"
    assert s.in_flight.ref_kind == "task"
    assert s.in_flight.tool == "implement_feature"
    assert s.in_flight.is_done_check is False
    assert s.in_flight.is_discovery is False


@pytest.mark.asyncio
async def test_sweep_does_not_readopt_settled_task(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.record_settlement("g", ref_id="t-seen", ref_kind="task", status="done")

    engine = _TaskFinderEngine(task=("t-seen", "add /health", "implement_feature"))
    swept = await sweep_orphaned_refs(store, engine)

    assert swept == {}


# ---- 8. _run_atomic rejects a yielding coroutine ----------------------------


@pytest.mark.asyncio
async def test_run_atomic_rejects_yielding_coroutine(tmp_path):
    """A hypothetical remote/HTTP engine that genuinely suspends inside
    dispatch() must fail LOUD (RuntimeError), not silently hold the shared
    transaction open across a real suspend point — such an engine cannot be
    atomic and must not pretend to be."""

    async def _yields():
        await asyncio.sleep(0)  # a REAL suspend point
        return InFlight("devclaw", "implement_feature", "unreachable", "task")

    with pytest.raises(RuntimeError, match="yielded inside the atomic dispatch transaction"):
        _run_atomic(_yields())

    # End-to-end: a dispatch site whose engine does this rolls its
    # transaction back cleanly and follows the ordinary dispatch-error
    # recovery path (ERROR outcome, no in_flight left dangling).
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    class _YieldingEngine(FakeEngine):
        async def dispatch(self, action, goal, notify_url):
            await asyncio.sleep(0)
            return await super().dispatch(action, goal, notify_url)

    out = await tick_goal(
        "g", store=store, engine=_YieldingEngine(),
        planner_caller=FakeClaude(ACT_FEATURE), evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
    )

    assert out is Outcome.ERROR
    s = store.load_status("g")
    assert s.in_flight is None
    assert s.phase == "idle"
