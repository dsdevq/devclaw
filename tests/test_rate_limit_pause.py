"""Quota/rate-limit pause — a usage limit pauses dispatch + requeues, never
fails-and-retries (which would burn the remaining quota on the same doomed call).
Driven with stub runners (no docker)."""
from __future__ import annotations

import pytest

from devclaw import task_queue
from devclaw.loom import limits  # the constant's real home post-extraction
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore, _now_ms
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


async def test_rate_limit_pauses_not_fails(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []

    async def rl(req: EngineRequest):
        calls.append(req.goal)
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "pending"          # requeued, NOT failed
    assert len(calls) == 1                # NOT retried — quota not burned
    until, reason = store.global_pause()
    assert until > _now_ms() and "rate_limit" in reason


async def test_real_error_still_fails(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    async def boom(req: EngineRequest):
        return {"status": "error", "error": "ModuleNotFoundError: No module named 'fastapi'"}

    q = TaskQueue(store, runner=boom)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    assert store.get_task(tid).status == "failed"  # real bug → fails as before
    assert store.global_pause()[0] == 0            # no pause set


async def test_pump_holds_dispatch_while_paused(store):
    called: list = []

    async def ok(req: EngineRequest):
        called.append(1)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    store.set_global_pause(_now_ms() + 60_000, "manual")  # paused 60s out
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    assert store.get_task(tid).status == "pending"  # dispatch held
    assert called == []


async def test_resumes_after_pause_expires(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    state = {"n": 0}

    async def rl_then_ok(req: EngineRequest):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": "error", "error": "rate limit exceeded"}
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=rl_then_ok)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    assert store.get_task(tid).status == "pending"      # paused after the rl hit

    # simulate the pause window elapsing, then re-pump
    store.set_global_pause(_now_ms() - 1000, "expired")  # in the past
    q._pump()
    await q.drain()

    assert store.get_task(tid).status == "done"          # auto-resumed + completed
    assert state["n"] == 2
    assert store.global_pause()[0] == 0                   # pause cleared on resume


async def test_retry_after_hint_caps_to_max(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    monkeypatch.setattr(limits, "RATE_LIMIT_MAX_PAUSE_S", 60)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "usage limit — try again in 10 hours"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    until, _ = store.global_pause()
    # 10h hint is capped to the 60s max so we re-probe promptly, not in 10h
    assert until <= _now_ms() + 60_000 + 2_000
