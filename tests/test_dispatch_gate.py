"""Operator dispatch controls — the manual pause toggle and the daily run-window.

Pure gate math (dispatch_gate) + StateStore round-trip. No docker, no cloud
model: exercises the module directly the way the two heartbeat gates call it."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from devclaw import dispatch_gate as gate
from devclaw.state_store import StateStore


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ms(y, mo, d, h, mi, tz="UTC") -> int:
    """Epoch-ms for a local wall-clock in ``tz`` — mirrors how the gate is fed."""
    dt = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz))
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


# ---- within_window --------------------------------------------------------

@pytest.mark.parametrize("now,start,end,inside", [
    (9 * 60, "09:00", "18:00", True),        # open edge is inclusive
    (18 * 60, "09:00", "18:00", False),      # close edge is exclusive
    (12 * 60, "09:00", "18:00", True),
    (8 * 60 + 59, "09:00", "18:00", False),
    (23 * 60, "22:00", "06:00", True),       # overnight — after start
    (3 * 60, "22:00", "06:00", True),        # overnight — before end
    (12 * 60, "22:00", "06:00", False),      # overnight — midday is out
])
def test_within_window(now, start, end, inside):
    assert gate.within_window(now, start, end) is inside


@pytest.mark.parametrize("start,end", [
    ("bad", "18:00"), ("09:00", "99:99"), ("09:00", "09:00"),  # malformed / zero-width
])
def test_within_window_fails_open(start, end):
    assert gate.within_window(10 * 60, start, end) is True


# ---- schedule_blocks (timezone-aware) -------------------------------------

def test_schedule_disabled_never_blocks():
    s = {"enabled": False, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.schedule_blocks(s, _ms(2026, 7, 5, 3, 0)) == (False, "")


def test_schedule_blocks_outside_window():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    blocked, reason = gate.schedule_blocks(s, _ms(2026, 7, 5, 20, 0))
    assert blocked and "run window" in reason


def test_schedule_allows_inside_window():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.schedule_blocks(s, _ms(2026, 7, 5, 12, 0)) == (False, "")


def test_schedule_respects_timezone():
    # July: Kyiv is UTC+3. 12:00 UTC = 15:00 Kyiv (inside 09–18);
    # 05:00 UTC = 08:00 Kyiv (before 09:00 → outside).
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "Europe/Kyiv"}
    assert gate.schedule_blocks(s, _ms(2026, 7, 5, 12, 0, "UTC"))[0] is False
    assert gate.schedule_blocks(s, _ms(2026, 7, 5, 5, 0, "UTC"))[0] is True


def test_schedule_bad_tz_fails_open():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "Nowhere/Nope"}
    assert gate.schedule_blocks(s, _ms(2026, 7, 5, 20, 0)) == (False, "")


# ---- operator_block (manual hold precedence) ------------------------------

def test_hold_wins_over_open_window():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    blocked, reason = gate.operator_block((True, "manual"), s, _ms(2026, 7, 5, 12, 0))
    assert blocked and reason == "manual"


def test_no_hold_falls_through_to_schedule():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.operator_block((False, ""), s, _ms(2026, 7, 5, 12, 0)) == (False, "")
    assert gate.operator_block((False, ""), s, _ms(2026, 7, 5, 20, 0))[0] is True


def test_hold_default_reason():
    assert gate.operator_block((True, ""), {}, 0) == (True, "operator pause")


# ---- StateStore round-trip ------------------------------------------------

def test_operator_hold_roundtrip(store):
    assert store.operator_hold() == (False, "")
    store.set_operator_hold(True, "stopping for the night")
    assert store.operator_hold() == (True, "stopping for the night")
    store.set_operator_hold(False)
    assert store.operator_hold() == (False, "")


def test_run_schedule_default_and_roundtrip(store):
    assert store.get_run_schedule() == gate.DEFAULT_SCHEDULE
    store.set_run_schedule(True, "08:00", "20:00", "Europe/Kyiv")
    assert store.get_run_schedule() == {
        "enabled": True, "start": "08:00", "end": "20:00", "tz": "Europe/Kyiv",
    }


def test_run_schedule_corrupt_falls_back(store):
    store.set_meta("run_schedule", "{not valid json")
    assert store.get_run_schedule() == gate.DEFAULT_SCHEDULE


# ---- wiring: the real heartbeat gates honour the store flags --------------

async def test_pump_holds_then_resumes_under_operator_hold(store):
    """The real TaskQueue._pump must skip launches while held, and resume once
    cleared — proves the gate is wired into the queue, not just the pure fn."""
    from devclaw.engine import EngineRequest
    from devclaw.task_queue import TaskQueue

    launched: list = []

    async def ok(req: EngineRequest):
        launched.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    store.set_operator_hold(True, "held")
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")  # submit pumps once
    await q.drain()
    assert launched == []  # nothing launched while the manual hold is on

    store.set_operator_hold(False)
    q._pump()  # the next heartbeat tick after unpause
    await q.drain()
    assert launched == ["g"]  # resumes once cleared


def test_engine_operator_block_reflects_store(store):
    """The goal side reads the same flags via the engine delegate + tick helper."""
    from devclaw.goal.engine import InProcessEngine
    from devclaw.goal.tick import _engine_operator_block
    from devclaw.state_store import _now_ms
    from devclaw.task_queue import TaskQueue

    eng = InProcessEngine(TaskQueue(store), store)
    assert _engine_operator_block(eng) == (False, "")
    store.set_operator_hold(True, "manual")
    blocked, reason = _engine_operator_block(eng)
    assert blocked and reason == "manual"
    assert eng.operator_block(_now_ms())[0] is True
