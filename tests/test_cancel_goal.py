"""cancel_goal: terminal-state transitions and graceful no-ops."""
from __future__ import annotations

import pytest

from devclaw.goal.models import GoalStatus, InFlight
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal


@pytest.fixture()
def db(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _svc(tmp_path, db):
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir,
        notify_url="",
        tick_seconds=900,
        eval_every=3,
        verify_done=False,
    )
    queue = TaskQueue(db)
    return GoalService(queue, db, config=cfg), queue, goals_dir


# ---- (a) active goal → cancelled --------------------------------------------


def test_cancel_idle_goal_transitions_to_cancelled(tmp_path, db):
    """Cancelling a non-terminal (idle) goal writes phase='cancelled'."""
    svc, _, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="idle"))

    result = svc.cancel_goal("g")

    assert result["cancelled"] is True
    assert result["phase"] == "cancelled"
    assert svc._goal_store.load_status("g").phase == "cancelled"


def test_cancel_in_flight_goal_also_cancels_engine_task(tmp_path, db):
    """Cancelling an in_flight goal tears down its in-flight task in the engine."""
    svc, queue, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    # Insert the task row directly to avoid the asyncio.ensure_future call in
    # queue.submit/_pump (there is no event loop in a sync test).
    task_id = "task-abc"
    db.create_task(id=task_id, kind="implement_feature", workspace_dir="/ws", goal="do x")
    svc._goal_store.save_status("g", GoalStatus(
        phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", task_id, "task", "g"),
    ))

    result = svc.cancel_goal("g")

    assert result["cancelled"] is True
    assert svc._goal_store.load_status("g").phase == "cancelled"
    assert db.get_task(task_id).status == "cancelled"


# ---- (b) already-terminal goal → graceful no-op -----------------------------


def test_cancel_done_goal_returns_graceful_response(tmp_path, db):
    """Cancelling a done goal returns cancelled=False and leaves the phase intact."""
    svc, _, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="done"))

    result = svc.cancel_goal("g")

    assert result["cancelled"] is False
    assert result["phase"] == "done"
    assert "terminal" in result.get("reason", "")
    assert svc._goal_store.load_status("g").phase == "done"


def test_cancel_already_cancelled_goal_returns_graceful_response(tmp_path, db):
    """cancel_goal is idempotent — a second call on a cancelled goal is a graceful no-op."""
    svc, _, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="cancelled"))

    result = svc.cancel_goal("g")

    assert result["cancelled"] is False
    assert result["phase"] == "cancelled"
    assert "terminal" in result.get("reason", "")


def test_cancel_goal_unknown_raises_key_error(tmp_path, db):
    """cancel_goal raises KeyError for unknown goal IDs (mirrors get_goal/steer_goal)."""
    svc, _, _ = _svc(tmp_path, db)
    with pytest.raises(KeyError):
        svc.cancel_goal("no-such-goal")


# ---- cancelled goal is skipped on the next tick (zero tokens) ---------------


@pytest.mark.asyncio
async def test_cancelled_goal_is_skipped_on_tick(tmp_path):
    """A cancelled goal must be skipped on every future tick — same zero-token
    guard as done, so it never burns quota on a permanently-stopped goal."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="cancelled"))
    planner = FakeClaude("{}")
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="", prepare_ws=fake_prepare, eval_every=99,
    )

    assert out is Outcome.SKIP_CANCELLED
    assert planner.calls == 0 and evaluator.calls == 0
    assert engine.dispatched == []
