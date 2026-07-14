"""Tranche 1 / PR2 substrate — StateStore.transaction() atomicity, the
_commit() seam, the (unused) GoalState tables, and the GoalStore wiring.

Nothing here asserts a behavior change to existing paths — it pins the NEW
transaction capability and that the goal-state tables get bootstrapped. The
rest of the suite is the guard that existing single-write behavior is unchanged.
"""

from __future__ import annotations

import sqlite3

import pytest

from devclaw.goal.state import GoalState
from devclaw.goal.store import GoalStore
from devclaw.state_store import StateStore

GOAL_STATE_TABLES = {
    "goal_status",
    "goal_phase_history",
    "goal_steering",
    "goal_log",
    "goal_deliveries",
    "goal_settlements",
    "goal_docs",
}


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "state.db")


@pytest.fixture()
def store(db_path):
    s = StateStore(db_path)
    yield s
    s.close()


def _peek_task_count(db_path: str) -> int:
    """Task count as seen by a SEPARATE connection — WAL means it observes only
    COMMITTED rows, so it is a faithful probe of what has actually been flushed
    (uncommitted rows inside an open transaction() are invisible to it)."""
    c = sqlite3.connect(db_path)
    try:
        return int(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    finally:
        c.close()


def _mk(store: StateStore, tid: str, goal_id: str | None = None) -> None:
    store.create_task(
        id=tid,
        kind="implement_feature",
        workspace_dir="/ws",
        goal="g",
        parent_goal_id=goal_id,
    )


# ---- transaction(): single commit at depth 0 -----------------------------


def test_transaction_commits_once_at_depth_zero(store, db_path):
    assert _peek_task_count(db_path) == 0
    with store.transaction():
        _mk(store, "t1")
        _mk(store, "t2")
        # Nothing is flushed mid-transaction — a separate connection still sees 0.
        assert _peek_task_count(db_path) == 0
        assert store._txn_depth == 1
    # A SINGLE commit on the outermost exit lands both rows together.
    assert _peek_task_count(db_path) == 2
    assert store._txn_depth == 0


def test_nested_transaction_does_not_precommit(store, db_path):
    with store.transaction():
        _mk(store, "outer")
        with store.transaction():
            _mk(store, "inner")
            assert store._txn_depth == 2
            # Inner block hasn't committed anything — it joined the outer unit.
            assert _peek_task_count(db_path) == 0
        # Back at the outer depth; the inner exit did NOT pre-commit.
        assert store._txn_depth == 1
        assert _peek_task_count(db_path) == 0
    assert store._txn_depth == 0
    # One commit at depth 0 flushes both the outer and inner writes together.
    assert _peek_task_count(db_path) == 2


def test_transaction_rolls_back_whole_unit_on_exception(store, db_path):
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            _mk(store, "a")
            _mk(store, "b")
            raise RuntimeError("boom")
    # Every write in the unit is rolled back — none survive.
    assert _peek_task_count(db_path) == 0
    assert store.get_task("a") is None
    assert store.get_task("b") is None
    assert store._txn_depth == 0
    assert store._txn_failed is False


def test_nested_exception_rolls_back_even_when_caught_between_levels(store, db_path):
    """An exception passing through any depth dooms the whole unit — the outer
    level rolls back even if it swallows the inner error."""
    with store.transaction():
        _mk(store, "outer")
        try:
            with store.transaction():
                _mk(store, "inner")
                raise ValueError("inner failed")
        except ValueError:
            pass  # caught, but the unit is already doomed
        assert store._txn_failed is True
    # Outermost exit saw the failure flag and rolled everything back.
    assert _peek_task_count(db_path) == 0
    assert store.get_task("outer") is None
    assert store._txn_depth == 0


# ---- unchanged single-write behavior outside a transaction ----------------


def test_create_task_outside_transaction_commits_immediately(store, db_path):
    _mk(store, "solo")
    # Visible to a separate connection at once — the pre-existing commit-per-call
    # behavior is preserved when no transaction() is open.
    assert _peek_task_count(db_path) == 1
    assert store.get_task("solo") is not None


def test_create_task_inside_failed_transaction_is_rolled_back(store, db_path):
    """The atomic-unit property a later dispatch PR needs: a task row created
    inside a transaction() that then raises must NOT persist."""
    with pytest.raises(ValueError):
        with store.transaction():
            _mk(store, "doomed")
            assert store.get_task("doomed") is not None  # visible within the unit
            raise ValueError("dispatch failed after create_task")
    assert store.get_task("doomed") is None
    assert _peek_task_count(db_path) == 0


# ---- latest_task_for_goal read helper -------------------------------------


def test_latest_task_for_goal(store, monkeypatch):
    # Patch the binding core.py actually calls (`from .rows import _now_ms`),
    # not the package re-export — patching devclaw.state_store._now_ms leaves
    # create_task on the real clock, and two creates in the same millisecond
    # tie on created_at and come back in arbitrary order (flaked on the fast
    # arm64 CI runner while passing locally).
    import devclaw.state_store.core as ss_core

    ticks = iter(range(1000, 1_000_000, 1000))
    monkeypatch.setattr(ss_core, "_now_ms", lambda: next(ticks))

    assert store.latest_task_for_goal("g1") is None
    _mk(store, "t1", goal_id="g1")
    _mk(store, "t2", goal_id="g1")
    _mk(store, "other", goal_id="g2")

    latest = store.latest_task_for_goal("g1")
    assert latest is not None
    assert latest.id == "t2"  # most recent by created_at
    assert store.latest_task_for_goal("nope") is None


# ---- GoalState bootstrap (tables only, idempotent) ------------------------


def test_goal_state_bootstrap_is_idempotent(store):
    GoalState(store)
    GoalState(store)  # second construction must not raise
    names = {
        r["name"]
        for r in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert GOAL_STATE_TABLES <= names


# ---- GoalStore seam -------------------------------------------------------


def test_goal_store_self_creates_isolated_state(tmp_path):
    gs = GoalStore(tmp_path)
    try:
        assert isinstance(gs._goal_state, GoalState)
        # A private db was created beside the goals; its tables are bootstrapped.
        assert (tmp_path / ".goal-state.db").exists()
        names = {
            r["name"]
            for r in gs._state._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "goal_status" in names
        # Smoke: ordinary store behavior is untouched, and the private db file
        # is not mistaken for a goal.
        gs.create_goal("g1", objective="do the thing", workspace_dir="/ws")
        assert gs.list_goal_ids() == ["g1"]
    finally:
        gs._state.close()


def test_goal_store_uses_shared_state_when_given(tmp_path, store):
    gs = GoalStore(tmp_path, state=store)
    assert gs._state is store
    # No private db self-created when a shared store is provided.
    assert not (tmp_path / ".goal-state.db").exists()
    names = {
        r["name"]
        for r in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    # Goal-state and task tables share the one database.
    assert "goal_status" in names
    assert "tasks" in names
