"""Retry-on-fail — the third leg of verify + RETRY + human.

A task that fails its verify gate (or errors) is re-run, each time with the
failure fed back into the goal, up to DEVCLAW_MAX_RETRIES, then escalated.
Timeouts are NOT retried. Driven with stub runners (no docker).
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


def _gate(passed: bool, output: str = ""):
    return {"ran": True, "cmd": "pytest", "passed": passed,
            "exit_code": 0 if passed else 1, "timed_out": False, "output": output}


def _flaky_runner(fail_times: int, calls: list):
    """Agent-ok every time, but the gate fails the first `fail_times` runs."""
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = _gate(passed=len(calls) > fail_times, output="boom-detail")
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


async def test_retry_then_success_feeds_failure_back(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(store, runner=_flaky_runner(fail_times=1, calls=calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="do X", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 2  # first failed the gate, retried, second passed
    # the retry goal carried the failure context forward
    assert calls[0] == "do X"
    assert "[Automatic retry 1/1]" in calls[1] and "boom-detail" in calls[1] and "do X" in calls[1]


async def test_retries_exhausted_then_failed(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(store, runner=_flaky_runner(fail_times=99, calls=calls))  # never passes
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert len(calls) == 2  # 1 attempt + 1 retry
    assert "failed after 2 attempts" in t.error and "boom-detail" in t.error


async def test_no_retry_when_disabled(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    calls: list = []
    q = TaskQueue(store, runner=_flaky_runner(fail_times=99, calls=calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "failed"
    assert len(calls) == 1  # no retry


async def test_success_first_try_runs_once(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(store, runner=_flaky_runner(fail_times=0, calls=calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 1  # no needless retry on success


async def test_timeout_is_not_retried(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    monkeypatch.setattr(task_queue, "TASK_TIMEOUT_S", 0.2)
    calls: list = []

    async def slow(req: EngineRequest):
        calls.append(req.goal)
        await asyncio.sleep(5)  # >> the 0.2s cap
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=slow)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed" and "wall-clock timeout" in t.error
    assert len(calls) == 1  # a stuck run is escalated, not retried
