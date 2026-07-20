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


# ---- per-goal run-window (an extra narrowing on top of the global one) -----

def test_per_goal_schedule_roundtrip_and_isolation(store):
    """A goal's own window is stored + read independently of the global one, and
    listing surfaces only the per-goal windows."""
    assert store.get_run_schedule("g") == gate.DEFAULT_SCHEDULE   # unset → disabled default
    store.set_run_schedule(True, "22:00", "06:00", "Europe/Kyiv", goal_id="g")
    assert store.get_run_schedule("g") == {
        "enabled": True, "start": "22:00", "end": "06:00", "tz": "Europe/Kyiv",
    }
    assert store.get_run_schedule() == gate.DEFAULT_SCHEDULE      # global untouched
    assert store.list_goal_schedules() == {"g": store.get_run_schedule("g")}


def test_per_goal_schedule_clear_falls_back(store):
    store.set_run_schedule(True, "22:00", "06:00", "UTC", goal_id="g")
    store.clear_run_schedule("g")
    assert store.get_run_schedule("g") == gate.DEFAULT_SCHEDULE
    assert store.list_goal_schedules() == {}


def test_list_goal_schedules_ignores_global_key(store):
    """The global ``run_schedule`` meta key must not leak into the per-goal list."""
    store.set_run_schedule(True, "09:00", "18:00", "UTC")            # global
    store.set_run_schedule(True, "22:00", "06:00", "UTC", "night")   # per-goal
    assert list(store.list_goal_schedules()) == ["night"]


def test_engine_goal_operator_block_reflects_store(store):
    """The per-goal gate is schedule-only and independent of the global gate:
    a goal outside its own window blocks even with no global hold/window set."""
    from devclaw.goal.engine import InProcessEngine
    from devclaw.goal.tick import _engine_goal_operator_block
    from devclaw.task_queue import TaskQueue

    eng = InProcessEngine(TaskQueue(store), store)
    noon = _ms(2026, 7, 5, 12, 0)
    assert eng.goal_operator_block("g", noon) == (False, "")     # no per-goal window
    store.set_run_schedule(True, "00:00", "00:30", "UTC", goal_id="g")  # closed at noon
    blocked, reason = eng.goal_operator_block("g", noon)
    assert blocked and "run window" in reason
    assert eng.operator_block(noon) == (False, "")               # global still open


def test_engine_goal_operator_block_open_for_test_doubles():
    """A test double lacking the method reads open (existing fakes tick unchanged)."""
    from devclaw.goal.tick import _engine_goal_operator_block

    assert _engine_goal_operator_block(object(), "g") == (False, "")


# ---- next_window_open_ms (the "held until when" legibility helper) ---------

def test_next_window_open_is_none_when_open_or_disabled():
    open_s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.next_window_open_ms(open_s, _ms(2026, 7, 5, 12, 0)) is None
    off = {"enabled": False, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.next_window_open_ms(off, _ms(2026, 7, 5, 3, 0)) is None
    bad_tz = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "Mars/Olympus"}
    assert gate.next_window_open_ms(bad_tz, _ms(2026, 7, 5, 3, 0)) is None  # fails open


def test_next_window_open_same_day():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.next_window_open_ms(s, _ms(2026, 7, 5, 3, 0)) == _ms(2026, 7, 5, 9, 0)


def test_next_window_open_rolls_to_tomorrow():
    s = {"enabled": True, "start": "09:00", "end": "18:00", "tz": "UTC"}
    assert gate.next_window_open_ms(s, _ms(2026, 7, 5, 20, 0)) == _ms(2026, 7, 6, 9, 0)


def test_next_window_open_overnight_window_in_tz():
    """The 2026-07-20 incident shape: window 22:00–05:00 Europe/London, quota
    reset lands 06:02 UTC (07:02 London, closed) → next open is 22:00 London
    that evening = 21:00 UTC in summer."""
    s = {"enabled": True, "start": "22:00", "end": "05:00", "tz": "Europe/London"}
    now = _ms(2026, 7, 20, 6, 2)                                   # 06:02 UTC
    assert gate.next_window_open_ms(s, now) == _ms(2026, 7, 20, 22, 0, tz="Europe/London")
