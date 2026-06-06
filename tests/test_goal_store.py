"""Goal-layer durable-mind round-trips and cadence math (folded from goalclaw)."""

from __future__ import annotations

import pytest

from devclaw.goal_models import GoalStatus, InFlight
from devclaw.goal_store import GoalStore, parse_duration
from tests.goal_fakes import Clock, seed_goal


def test_parse_duration():
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    with pytest.raises(ValueError):
        parse_duration("nonsense")


def test_load_goal(tmp_path):
    seed_goal(tmp_path, "g1", backlog=["x", "y"])
    store = GoalStore(tmp_path)
    g = store.load_goal("g1")
    assert g.id == "g1"
    assert g.engine == "devclaw"
    assert g.workspace_dir == "/repos/demo"
    assert g.backlog == ["x", "y"]
    assert g.open_pr is True


def test_create_goal_writes_and_rejects_dupes(tmp_path):
    store = GoalStore(tmp_path)
    g = store.create_goal(
        "newg", objective="ship the thing", workspace_dir="/ws",
        done_when="it works", backlog=["a", "b"], cadence="6h",
    )
    assert g.objective == "ship the thing"
    assert store.exists("newg")
    assert store.load_goal("newg").backlog == ["a", "b"]
    with pytest.raises(FileExistsError):
        store.create_goal("newg", objective="dup", workspace_dir="/ws")


def test_status_roundtrip_with_eval_and_done_check(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    s = GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "t9", "task", "verify", is_done_check=True),
        next="verifying done",
        last_plan_at="2026-06-06T12:00:00+00:00",
        inbox_cursor=2,
        deliveries_since_eval=3,
        last_eval_verdict="on_track",
        last_eval_note="progressing",
    )
    store.save_status("g1", s)
    back = store.load_status("g1")
    assert back.phase == "verifying"
    assert back.in_flight is not None
    assert back.in_flight.id == "t9"
    assert back.in_flight.is_done_check is True
    assert back.inbox_cursor == 2
    assert back.deliveries_since_eval == 3
    assert back.last_eval_verdict == "on_track"


def test_missing_status_is_default(tmp_path):
    store = GoalStore(tmp_path)
    s = store.load_status("never")
    assert s.phase == "idle"
    assert s.in_flight is None
    assert s.deliveries_since_eval == 0


def test_log_append_and_recent(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_log("g1", "first")
    store.append_log("g1", "second")
    recent = store.recent_log("g1")
    assert "first" in recent and "second" in recent
    assert recent.index("first") < recent.index("second")  # newest at bottom


def test_deliveries_roundtrip(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_delivery("g1", "add /health", "PR: #7\nAgent summary: added endpoint\nVerify: PASSED")
    store.append_delivery("g1", "add logging", "PR: #8")
    d = store.recent_deliveries("g1")
    assert "add /health" in d and "#7" in d and "add logging" in d


def test_inbox_cursor_and_steering_sources(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_steering("g1", ["focus on auth first"], source="denys")
    s0 = store.load_status("g1")  # cursor 0
    assert "focus on auth first" in store.unread_steering("g1", s0)
    cursor = store.steering_cursor("g1")
    assert cursor == 1
    s1 = GoalStatus(inbox_cursor=cursor)
    assert store.unread_steering("g1", s1) == ""
    # evaluator appends a correction → becomes fresh steering
    store.append_steering("g1", ["redo the rate limiter per-user"], source="auto-eval")
    fresh = store.unread_steering("g1", s1)
    assert "rate limiter" in fresh and "auto-eval" in fresh


def test_cadence_due(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g1", cadence="6h")
    goal = store.load_goal("g1")
    assert store.cadence_due(goal, GoalStatus(last_plan_at=None)) is True
    just_now = store.now_iso()
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is False
    clock.advance(6 * 3600 + 1)
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is True
