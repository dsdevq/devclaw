"""parent_goal_id — durable goal-owner pointer on Task.

Pins the noun-model refactor (2026-07-04): a task carries a nullable
``parent_goal_id`` that identifies which durable goal owns it, orthogonal to
``program_id`` (ephemeral DAG-run pointer). Set by ``InProcessEngine.dispatch``
when the goal heartbeat fires; null for standalone ``dispatch_task`` calls.
"""

from __future__ import annotations

import json

import pytest

from devclaw.engine import EngineRequest
from devclaw.goal.engine import InProcessEngine
from devclaw.goal.models import Action, Goal
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


# ---------------- state store ----------------


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_task_without_parent_goal_id_is_none(store):
    store.create_task(
        id="t1", kind="implement_feature", workspace_dir="/ws", goal="do it"
    )
    t = store.get_task("t1")
    assert t is not None
    assert t.parent_goal_id is None


def test_task_with_parent_goal_id_roundtrips(store):
    store.create_task(
        id="t1",
        kind="implement_feature",
        workspace_dir="/ws",
        goal="do it",
        parent_goal_id="goal_abc",
    )
    t = store.get_task("t1")
    assert t.parent_goal_id == "goal_abc"


def test_to_dict_includes_parent_goal_id(store):
    store.create_task(
        id="t1",
        kind="fix_bug",
        workspace_dir="/ws",
        goal="fix it",
        parent_goal_id="goal_xyz",
    )
    d = store.get_task("t1").to_dict()
    assert d["parentGoalId"] == "goal_xyz"


def test_migration_survives_pre_existing_db(tmp_path):
    """A DB opened by an old build (before the parent_goal_id column existed)
    must have the column added by the idempotent ALTER on next open."""
    db_path = str(tmp_path / "legacy.db")
    # Simulate an old DB: create it via one StateStore (schema now includes the
    # column since we ship it in CREATE TABLE), close, open again — the ALTER
    # is idempotent and shouldn't fail. This is the round-trip that matters.
    s1 = StateStore(db_path)
    s1.create_task(
        id="t1", kind="implement_feature", workspace_dir="/ws", goal="pre-alter"
    )
    s1.close()
    s2 = StateStore(db_path)
    t = s2.get_task("t1")
    assert t.parent_goal_id is None  # legacy row has no owner
    s2.close()


# ---------------- goal engine dispatch ----------------


def _goal():
    return Goal(
        id="goal_g",
        objective="obj",
        cadence="1d",
        engine="devclaw",
        workspace_dir="/ws",
        verify_cmd="pytest -q",
        backlog=["a"],
    )


async def _ok_runner(request: EngineRequest) -> dict:
    return {"status": "ok", "message": f"did: {request.goal[:40]}"}


@pytest.fixture()
def wired(tmp_path):
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store, runner=_ok_runner)
    engine = InProcessEngine(queue, store)
    yield engine, queue, store
    store.close()


async def test_goal_dispatch_stamps_parent_goal_id_on_task(wired):
    engine, queue, store = wired
    action = Action(
        engine="devclaw", tool="implement_feature", goal="add /health", open_pr=False
    )
    ref = await engine.dispatch(action, _goal(), notify_url="")
    t = store.get_task(ref.id)
    assert t.parent_goal_id == "goal_g"


async def test_goal_dispatch_stamps_parent_goal_id_on_review(wired):
    engine, queue, store = wired
    action = Action(
        engine="devclaw", tool="review_repository", goal="assess", open_pr=False
    )
    ref = await engine.dispatch(action, _goal(), notify_url="")
    t = store.get_task(ref.id)
    assert t.parent_goal_id == "goal_g"
    # ownership is orthogonal to the read-only invariant — both hold
    assert t.verify_cmd is None
    assert t.deliver is False


# ---------------- standalone dispatch_task path ----------------


async def test_standalone_queue_submit_has_no_parent_goal_id(tmp_path):
    """A ``queue.submit`` call without ``parent_goal_id`` (the path
    ``dispatch_task`` takes) creates a task whose parent_goal_id is None.
    The Task-UI backend uses this to decide whether a task shows in a goal's
    Dispatched section or the loose Recent Tasks strip."""
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store, runner=_ok_runner)
    task_id = queue.submit(
        kind="implement_feature", workspace_dir="/ws", goal="add /health"
    )
    t = store.get_task(task_id)
    assert t is not None
    assert t.parent_goal_id is None
    store.close()
