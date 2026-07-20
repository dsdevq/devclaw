"""dispatch_hold on the goal read surfaces — a held instance must SAY so.

Named regression for the 2026-07-20 silent window-hold: a quota pause expired
into a closed run-window and every read surface (get_goal / list_goals /
tail_goal) showed a healthy-looking `in_flight` goal with `blocked_on: null`
while the queue held all dispatch for hours. Mirrors the GoalService wiring
used by test_tail_goal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from devclaw.goal.models import GoalStatus
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore, _now_ms
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import seed_goal


@pytest.fixture()
def db(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _svc(tmp_path, db):
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir, notify_url="", tick_seconds=900,
        eval_every=3, verify_done=False,
    )
    queue = TaskQueue(db)
    return GoalService(queue, db, config=cfg), goals_dir


def _seed(svc, goals_dir, gid="g"):
    seed_goal(goals_dir, gid)
    svc._goal_store.save_status(gid, GoalStatus(phase="idle"))


def _closed_window_now() -> tuple[str, str]:
    """An enabled window guaranteed CLOSED right now (opens 3h out, 1h wide)."""
    now = datetime.now(tz=timezone.utc)
    return (now + timedelta(hours=3)).strftime("%H:%M"), (now + timedelta(hours=4)).strftime("%H:%M")


def test_surfaces_show_none_when_dispatch_flows(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir)
    assert svc.get_goal("g")["dispatch_hold"] is None
    assert svc.list_goals()[0]["dispatch_hold"] is None
    assert svc.tail_goal("g")["dispatch_hold"] is None


def test_run_window_hold_is_named_with_next_open(tmp_path, db):
    """The 2026-07-20 shape: closed window → every surface names the hold and
    when it lifts, instead of rendering as healthy idle."""
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir)
    start, end = _closed_window_now()
    db.set_run_schedule(True, start, end, "UTC")

    hold = svc.get_goal("g")["dispatch_hold"]
    assert hold["kind"] == "run_window"
    assert "run window" in hold["reason"]
    assert hold["until"]  # ISO timestamp of the next window-open
    assert svc.list_goals()[0]["dispatch_hold"]["kind"] == "run_window"
    assert svc.tail_goal("g")["dispatch_hold"]["kind"] == "run_window"


def test_quota_pause_hold_surfaces_with_until(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir)
    db.set_global_pause(_now_ms() + 3_600_000, "quota: out of extra usage")

    hold = svc.get_goal("g")["dispatch_hold"]
    assert hold["kind"] == "quota_pause"
    assert "quota" in hold["reason"]
    assert hold["until"]


def test_expired_quota_pause_does_not_surface(tmp_path, db):
    """An expired pause is the queue's to clear on its next tick — the read
    surface must not report it as an active hold."""
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir)
    db.set_global_pause(_now_ms() - 1000, "quota: stale")
    assert svc.get_goal("g")["dispatch_hold"] is None


def test_operator_hold_wins_and_is_named(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir)
    db.set_operator_hold(True, "consolidation freeze")

    hold = svc.get_goal("g")["dispatch_hold"]
    assert hold["kind"] == "operator_hold"
    assert hold["reason"] == "consolidation freeze"
    assert "until" not in hold  # a manual hold has no self-lifting time


def test_per_goal_window_surfaces_only_on_that_goal(tmp_path, db):
    """get_goal folds in the goal's OWN window; list_goals stays account-wide."""
    svc, goals_dir = _svc(tmp_path, db)
    _seed(svc, goals_dir, "night-goal")
    _seed(svc, goals_dir, "day-goal")
    start, end = _closed_window_now()
    db.set_run_schedule(True, start, end, "UTC", goal_id="night-goal")

    assert svc.get_goal("night-goal")["dispatch_hold"]["kind"] == "run_window"
    assert svc.get_goal("day-goal")["dispatch_hold"] is None
    for row in svc.list_goals():  # global gate is open — rows carry no hold
        assert row["dispatch_hold"] is None
