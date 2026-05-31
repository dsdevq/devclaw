"""Cancellation (deliberate abort) tests — the kill switch for tasks + programs.

Distinct from crash recovery (test_durability): here a client *intentionally*
aborts in-flight work. The invariants under test:
  - a pending task can be cancelled before it ever runs (engine never invoked);
  - a running task's live execution is torn down (the runner sees CancelledError);
  - 'cancelled' is terminal — a late settle can't override it, and crash
    recovery never resurrects it;
  - cancelling a program stops scheduling and tears down every running child;
  - cancelling one child sticky-cancels its program (a hole blocks dependents).
"""

import asyncio

import pytest

from devclaw.engine import EngineRequest
from devclaw.planner import PlannedTask
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _gated_runner():
    """A runner that blocks until released, recording starts + whether it was
    cancelled mid-flight. Returns (runner, started_event, release_event, state)."""
    started = asyncio.Event()
    release = asyncio.Event()
    state = {"cancelled": False, "seen": []}

    async def runner(req: EngineRequest):
        state["seen"].append(req.goal)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "done"}

    return runner, started, release, state


# ---- standalone task cancellation ----


async def test_cancel_pending_task_never_runs(store, monkeypatch):
    monkeypatch.setattr("devclaw.task_queue.GLOBAL_MAX_CONCURRENT", 1)
    runner, _started, release, state = _gated_runner()
    q = TaskQueue(store, runner=runner)
    a = q.submit(kind="implement_feature", workspace_dir="/ws", goal="a")
    b = q.submit(kind="implement_feature", workspace_dir="/ws", goal="b")
    # claim_pending runs synchronously inside _pump → DB state is settled now;
    # cap=1 holds b pending behind the running a.
    assert store.get_task(a).status == "running"
    assert store.get_task(b).status == "pending"

    assert q.cancel_task(b) is True
    assert store.get_task(b).status == "cancelled"

    release.set()
    await q.drain()

    assert state["seen"] == ["a"]  # b was cancelled before it could launch
    assert store.get_task(a).status == "done"
    assert any(e.type == "cancelled" for e in store.list_events(task_id=b))


async def test_cancel_running_task_tears_down_and_is_terminal(store):
    runner, started, _release, state = _gated_runner()
    q = TaskQueue(store, runner=runner)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert store.get_task(tid).status == "running"

    assert q.cancel_task(tid) is True
    await q.drain()

    assert state["cancelled"] is True  # the runner observed CancelledError
    assert store.get_task(tid).status == "cancelled"

    # A late settle must NOT clobber the terminal 'cancelled' state.
    store.mark_done(tid, "{}")
    assert store.get_task(tid).status == "cancelled"


async def test_cancel_already_terminal_task_is_noop(store):
    q = TaskQueue(store)
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.mark_done("t1", "{}")
    assert q.cancel_task("t1") is False
    assert store.get_task("t1").status == "done"


# ---- crash recovery must not resurrect an abort ----


async def test_recover_does_not_resurrect_cancelled(store):
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")  # running
    store.mark_task_cancelled("t1")  # deliberately aborted (terminal)

    q = TaskQueue(store)
    assert q.recover() == 0  # only 'running' rows get reaped — cancelled is terminal
    assert store.get_task("t1").status == "cancelled"


# ---- program cancellation ----


async def test_cancel_program_aborts_running_and_pending(store):
    runner, started, release, state = _gated_runner()
    q = TaskQueue(store, runner=runner)
    pid = q.start_planned_program(
        goal="big",
        workspace_dir="/ws",
        planned=[
            PlannedTask(key="x", goal="x", kind="implement_feature", depends_on_keys=[]),
            PlannedTask(key="y", goal="y", kind="implement_feature", depends_on_keys=["x"]),
        ],
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)
    by_goal = {t.goal: t for t in store.list_program_tasks(pid)}
    assert by_goal["x"].status == "running"
    assert by_goal["y"].status == "pending"  # gated on x

    assert q.cancel_program(pid) is True
    release.set()
    await q.drain()

    assert store.get_program(pid).status == "cancelled"
    statuses = {t.goal: t.status for t in store.list_program_tasks(pid)}
    assert statuses == {"x": "cancelled", "y": "cancelled"}
    assert "y" not in state["seen"]  # y never launched


async def test_cancel_program_is_noop_when_terminal(store):
    async def ok_runner(req: EngineRequest):
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "done"}

    q = TaskQueue(store, runner=ok_runner)
    pid = q.start_planned_program(
        goal="g",
        workspace_dir="/ws",
        planned=[PlannedTask(key="a", goal="a", kind="implement_feature", depends_on_keys=[])],
    )
    await q.drain()
    assert store.get_program(pid).status == "done"
    assert q.cancel_program(pid) is False


async def test_cancel_child_task_sticky_cancels_program(store, monkeypatch):
    monkeypatch.setattr("devclaw.task_queue.GLOBAL_MAX_CONCURRENT", 1)
    runner, started, release, _state = _gated_runner()
    q = TaskQueue(store, runner=runner)
    pid = q.start_planned_program(
        goal="g",
        workspace_dir="/ws",
        planned=[  # independent tasks; cap=1 means only x runs, y held pending
            PlannedTask(key="x", goal="x", kind="implement_feature", depends_on_keys=[]),
            PlannedTask(key="y", goal="y", kind="implement_feature", depends_on_keys=[]),
        ],
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    xid = next(t.id for t in store.list_program_tasks(pid) if t.goal == "x")
    assert store.get_task(xid).status == "running"

    # Cancel just the child — the program should sticky-cancel on the pump that
    # cancel_task triggers (a hole in the DAG blocks the rest).
    assert q.cancel_task(xid) is True
    assert store.get_program(pid).status == "cancelled"

    release.set()
    await q.drain()

    statuses = {t.goal: t.status for t in store.list_program_tasks(pid)}
    assert statuses == {"x": "cancelled", "y": "cancelled"}  # y swept too
    assert store.has_active_work() is False  # nothing left dangling
