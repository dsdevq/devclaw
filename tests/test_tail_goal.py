"""tail_goal: the deep read-only observability surface (deliveries + artifacts +
live in-flight events). Mirrors the GoalService wiring used by test_cancel_goal."""
from __future__ import annotations

import pytest

from devclaw.goal.models import GoalStatus, InFlight
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
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


def test_tail_goal_returns_artifacts_and_deliveries(tmp_path, db):
    """tail_goal surfaces the grounded deliveries tail + discovery/spec artifacts —
    the things get_goal omits."""
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="idle"))
    svc._goal_store.append_delivery("g", "add /health endpoint", "shipped it; gate green; PR #3")
    svc._goal_store.write_discovery("g", "current: bare repo. gap: no endpoints.")
    svc._goal_store.write_spec("g", "build a tiny FastAPI service")

    out = svc.tail_goal("g")

    assert out["id"] == "g"
    assert out["phase"] == "idle"
    assert "PR #3" in out["deliveries"]
    assert "no endpoints" in out["discovery"]
    assert "FastAPI" in out["spec"]
    assert out["live_events"] == []  # nothing in flight


def test_tail_goal_includes_live_inflight_events(tmp_path, db):
    """When a task is in flight, tail_goal returns the TAIL of its live event
    stream so the run is watchable without SSH."""
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    task_id = "task-abc"
    db.create_task(id=task_id, kind="implement_feature", workspace_dir="/ws", goal="do x")
    for i in range(5):
        db.append_event(
            task_id=task_id, program_id=None, type="ActionEvent",
            source="agent", payload_json=f'{{"step": {i}}}',
        )
    svc._goal_store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", task_id, "task", "g"),
    ))

    out = svc.tail_goal("g", event_limit=3)

    assert out["in_flight"]["id"] == task_id
    assert len(out["live_events"]) == 3  # tailed to the last 3 of 5
    assert out["live_events"][-1]["preview"] == '{"step": 4}'
    assert out["live_events"][0]["type"] == "ActionEvent"


def test_tail_goal_unknown_raises_key_error(tmp_path, db):
    svc, _ = _svc(tmp_path, db)
    with pytest.raises(KeyError):
        svc.tail_goal("no-such-goal")
