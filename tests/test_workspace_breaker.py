"""Per-workspace circuit-breaker — N task failures on the same workspace within
a sliding window trip a hold on dispatch for that workspace, without touching
other workspaces. Sibling of the global quota pause, but scoped. Trigger event
that named this: 2026-07-02 closeloop retry storm."""
from __future__ import annotations

import pytest

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore, _now_ms
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _tight_breaker(monkeypatch):
    """Threshold=2 / window=30s / hold=30s keeps the tests fast + deterministic.
    Production defaults (3/900s/1800s) are covered by the module constants."""
    monkeypatch.setattr(task_queue, "WORKSPACE_BREAK_THRESHOLD", 2)
    monkeypatch.setattr(task_queue, "WORKSPACE_BREAK_WINDOW_S", 30.0)
    monkeypatch.setattr(task_queue, "WORKSPACE_BREAK_HOLD_S", 30.0)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)  # one shot per submit


async def _submit_and_fail(q: TaskQueue, workspace_dir: str) -> str:
    tid = q.submit(kind="implement_feature", workspace_dir=workspace_dir, goal="g")
    await q.drain()
    return tid


async def test_threshold_failures_trip_workspace_break(store):
    async def boom(req: EngineRequest):
        return {"status": "error", "error": "ModuleNotFoundError: nothing"}

    q = TaskQueue(store, runner=boom)
    # 2 failures @ threshold=2 → break trips on the second.
    t1 = await _submit_and_fail(q, "/ws-a")
    assert store.get_task(t1).status == "failed"
    until, _ = store.get_workspace_break("/ws-a")
    assert until == 0  # not tripped yet after 1

    t2 = await _submit_and_fail(q, "/ws-a")
    assert store.get_task(t2).status == "failed"
    until, reason = store.get_workspace_break("/ws-a")
    assert until > _now_ms()
    assert "circuit-breaker" in reason and "/ws-a" in reason


async def test_break_holds_new_dispatch_to_that_workspace(store):
    dispatched: list[str] = []

    async def boom(req: EngineRequest):
        dispatched.append(req.workspace_dir)
        return {"status": "error", "error": "boom"}

    q = TaskQueue(store, runner=boom)
    await _submit_and_fail(q, "/ws-a")
    await _submit_and_fail(q, "/ws-a")
    # Break should now be tripped for /ws-a; a fresh submit stays pending.
    dispatched.clear()
    t3 = q.submit(kind="implement_feature", workspace_dir="/ws-a", goal="g3")
    await q.drain()
    assert store.get_task(t3).status == "pending"
    assert dispatched == []  # never launched


async def test_break_is_scoped_to_workspace_others_run(store):
    async def boom(req: EngineRequest):
        return {"status": "error", "error": "boom"}

    ran_b: list[str] = []

    async def ok(req: EngineRequest):
        ran_b.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    # Trip /ws-a using a boom runner, then wire an ok runner for a fresh queue
    # backed by the SAME store — the break persists in meta so it holds across
    # the swap, but /ws-b is untouched and should still dispatch.
    q1 = TaskQueue(store, runner=boom)
    await _submit_and_fail(q1, "/ws-a")
    await _submit_and_fail(q1, "/ws-a")
    assert store.get_workspace_break("/ws-a")[0] > _now_ms()

    q2 = TaskQueue(store, runner=ok)
    tb = q2.submit(kind="implement_feature", workspace_dir="/ws-b", goal="g-b")
    await q2.drain()
    assert store.get_task(tb).status == "done"
    assert ran_b == ["g-b"]
    # /ws-a is still held.
    assert store.get_workspace_break("/ws-a")[0] > _now_ms()


async def test_expired_break_auto_clears_and_resumes(store):
    async def boom(req: EngineRequest):
        return {"status": "error", "error": "boom"}

    ran_ok: list[str] = []

    async def ok(req: EngineRequest):
        ran_ok.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q1 = TaskQueue(store, runner=boom)
    await _submit_and_fail(q1, "/ws-a")
    await _submit_and_fail(q1, "/ws-a")
    # Force-expire the break as if HOLD_S had elapsed.
    store.set_workspace_break("/ws-a", _now_ms() - 1000, "expired")

    q2 = TaskQueue(store, runner=ok)
    ta = q2.submit(kind="implement_feature", workspace_dir="/ws-a", goal="resume")
    await q2.drain()

    assert store.get_task(ta).status == "done"
    assert ran_ok == ["resume"]
    # Expired break was lazily cleared, so meta doesn't grow with dead keys.
    assert store.get_workspace_break("/ws-a") == (0, "")


async def test_trip_emits_exactly_one_event_during_hold(store):
    async def boom(req: EngineRequest):
        return {"status": "error", "error": "boom"}

    q = TaskQueue(store, runner=boom)
    t1 = await _submit_and_fail(q, "/ws-a")
    t2 = await _submit_and_fail(q, "/ws-a")  # trips here
    # An extra failure while the break is active must NOT re-fire the event.
    # (Simulate by manually failing another submit via mark_failed — dispatch is
    # held, so we can't reach the runner while the break is active.)
    t3 = q.submit(kind="implement_feature", workspace_dir="/ws-a", goal="g3")
    # Directly mark it failed to model the "already-tripped, another failure
    # lands" case, then invoke the checker to prove it stays quiet.
    store.mark_failed(t3, "manual")
    q._check_and_trip_breaker("/ws-a", t3)

    trips = [
        e for e in store.list_events(task_id=t2)
        if e.type == "workspace_break_tripped"
    ]
    assert len(trips) == 1
    assert store.list_events(task_id=t1, ) == [
        e for e in store.list_events(task_id=t1)
        if e.type != "workspace_break_tripped"
    ]  # the first failure did NOT trip
    assert not any(
        e.type == "workspace_break_tripped" for e in store.list_events(task_id=t3)
    )


async def test_threshold_zero_disables(store, monkeypatch):
    monkeypatch.setattr(task_queue, "WORKSPACE_BREAK_THRESHOLD", 0)

    async def boom(req: EngineRequest):
        return {"status": "error", "error": "boom"}

    q = TaskQueue(store, runner=boom)
    for _ in range(5):
        await _submit_and_fail(q, "/ws-a")
    assert store.get_workspace_break("/ws-a") == (0, "")


async def test_manual_clear_reopens_dispatch(store):
    async def boom(req: EngineRequest):
        return {"status": "error", "error": "boom"}

    ran_ok: list[str] = []

    async def ok(req: EngineRequest):
        ran_ok.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q1 = TaskQueue(store, runner=boom)
    await _submit_and_fail(q1, "/ws-a")
    await _submit_and_fail(q1, "/ws-a")
    assert store.get_workspace_break("/ws-a")[0] > _now_ms()

    store.clear_workspace_break("/ws-a")

    q2 = TaskQueue(store, runner=ok)
    ta = q2.submit(kind="implement_feature", workspace_dir="/ws-a", goal="unpause")
    await q2.drain()
    assert store.get_task(ta).status == "done"
    assert ran_ok == ["unpause"]


async def test_list_workspace_breaks_returns_active_entries(store):
    store.set_workspace_break("/ws-x", _now_ms() + 60_000, "test")
    store.set_workspace_break("/ws-y", _now_ms() + 60_000, "test-y")
    entries = {ws: (until, reason) for ws, until, reason in store.list_workspace_breaks()}
    assert set(entries.keys()) == {"/ws-x", "/ws-y"}
    assert entries["/ws-y"][1] == "test-y"
