"""Durability + recovery tests — crash recovery, global cap, cheap-idle, heartbeat."""

import asyncio

import pytest

from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_runner(seen: list[str]):
    async def runner(req: EngineRequest):
        seen.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "done"}
    return runner


# ---- crash recovery ----


async def test_recover_resets_orphaned_running_and_resumes(store):
    # Simulate a crash: a task is left 'running' with no live execution behind it.
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    assert store.get_task("t1").status == "running"

    # A fresh process (new TaskQueue) recovers it.
    q = TaskQueue(store, runner=_ok_runner([]))
    n = q.recover()
    assert n == 1
    assert store.get_task("t1").status == "pending"
    # the reap is in the audit log
    assert any(e.type == "reaped" for e in store.list_events(task_id="t1"))

    # …and a pump resumes it to completion.
    q._pump()
    await q.drain()
    assert store.get_task("t1").status == "done"


async def test_recover_noop_when_nothing_orphaned(store):
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.mark_done("t1", "{}")  # terminal — not orphaned
    q = TaskQueue(store)
    assert q.recover() == 0
    assert store.get_task("t1").status == "done"


def test_recover_sweeps_orphaned_sandbox_containers(store, monkeypatch, capsys):
    # The row reset alone re-runs the task in a NEW container while the dead
    # process's ORIGINAL container keeps running (--rm dies with its docker
    # client) — recover() must also reap those, and say how many it got.
    from devclaw import task_queue as tq

    calls: list[bool] = []

    def fake_sweep() -> int:
        calls.append(True)
        return 3

    monkeypatch.setattr(tq, "sweep_orphan_sandboxes", fake_sweep)
    q = TaskQueue(store, runner=_ok_runner([]))
    q.recover()
    assert calls == [True]  # exactly one sweep per recover
    assert "reaped 3 orphaned sandbox container(s)" in capsys.readouterr().err


def test_recover_sweep_is_silent_when_nothing_leaked(store, monkeypatch, capsys):
    from devclaw import task_queue as tq

    monkeypatch.setattr(tq, "sweep_orphan_sandboxes", lambda: 0)
    q = TaskQueue(store, runner=_ok_runner([]))
    q.recover()
    assert "sandbox container" not in capsys.readouterr().err


# ---- cheap-idle guard ----


def test_has_active_work_false_when_empty(store):
    assert store.has_active_work() is False


def test_idle_pump_is_a_noop(store):
    q = TaskQueue(store)
    q._pump()  # must not raise, must not error on an empty store


def test_has_active_work_true_with_pending(store):
    store.create_task(id="t1", kind="fix_bug", workspace_dir="/ws", goal="g")
    assert store.has_active_work() is True


# ---- global concurrency cap / backpressure ----


async def test_global_cap_limits_concurrent_runs(store, monkeypatch):
    monkeypatch.setattr("devclaw.task_queue.GLOBAL_MAX_CONCURRENT", 1)
    gate = asyncio.Event()
    seen: list[str] = []

    async def gated(req: EngineRequest):
        seen.append(req.goal)
        await gate.wait()
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "done"}

    q = TaskQueue(store, runner=gated)
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="a")
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="b")

    # cap=1 → exactly one task may be running; the other is held pending (backpressure)
    assert store.count_running() == 1
    assert len(store.list_pending_standalone()) == 1

    gate.set()
    await q.drain()
    assert store.count_running() == 0
    assert seen == ["a", "b"]  # the second ran only after the first freed the slot


# ---- heartbeat ----


async def test_heartbeat_resumes_recovered_work(store):
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")  # orphaned running
    q = TaskQueue(store, runner=_ok_runner([]))
    q.recover()  # → pending
    q.start_ticking()  # first tick pumps immediately
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if store.get_task("t1").status == "done":
                break
    finally:
        await q.stop_ticking()
    assert store.get_task("t1").status == "done"
