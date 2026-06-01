"""Per-task wall-clock timeout — kills a hung run cleanly instead of burning quota.

The live smoke leaked a sandbox on a silent post-init hang; this is the guard.
Driven with a stub runner that sleeps, no docker: on timeout the runner coroutine
is cancelled (which, for the real engine, tears down the container) and the task
settles `failed` with a clear reason — not `done`, and not hung forever.
"""

import asyncio

import pytest

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _slow_runner(sleep_s: float, finished: dict):
    """A runner that takes `sleep_s` to produce a result; records whether it ran
    to completion (so a test can prove a timeout cancelled it mid-run)."""
    async def runner(req: EngineRequest):
        await asyncio.sleep(sleep_s)
        finished["done"] = True
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "slow ok"}
    return runner


async def test_task_exceeding_wall_clock_is_failed_not_hung(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_TIMEOUT_S", 0.2)
    finished: dict = {}
    q = TaskQueue(store, runner=_slow_runner(5.0, finished))  # >> the 0.2s cap
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "wall-clock timeout" in t.error
    assert finished.get("done") is not True  # the runner was cancelled mid-run


async def test_fast_task_is_not_reaped(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_TIMEOUT_S", 5.0)
    finished: dict = {}
    q = TaskQueue(store, runner=_slow_runner(0.0, finished))  # well under the cap
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "done"
    assert finished.get("done") is True


async def test_timeout_disabled_lets_a_long_task_finish(store, monkeypatch):
    # <=0 disables the cap entirely (e.g. for a long eval build).
    monkeypatch.setattr(task_queue, "TASK_TIMEOUT_S", 0.0)
    finished: dict = {}
    q = TaskQueue(store, runner=_slow_runner(0.3, finished))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert finished.get("done") is True
