"""The project registry — the control plane's source of truth for 'what is
devclaw working on'. CRUD over its own SQLite table + the live status rollup that
joins linked goals on read (never caching their phase)."""
from __future__ import annotations

import pytest

from devclaw.project_registry import (
    ProjectExists,
    ProjectRegistry,
    project_rollup,
)


@pytest.fixture
def reg(tmp_path):
    return ProjectRegistry(str(tmp_path / "devclaw.db"))


def test_create_get_list(reg):
    reg.create(id="todo", name="Todo App", repo_url="git@x/todo.git")
    reg.create(id="blog", name="Blog")
    assert {p.id for p in reg.list()} == {"todo", "blog"}
    p = reg.get("todo")
    assert p is not None and p.name == "Todo App" and p.repo_url == "git@x/todo.git"
    assert p.status == "active" and p.goal_ids == []


def test_duplicate_id_raises(reg):
    reg.create(id="todo", name="Todo")
    with pytest.raises(ProjectExists):
        reg.create(id="todo", name="Other")


def test_get_unknown_is_none(reg):
    assert reg.get("nope") is None


def test_update_is_partial_and_bumps_updated_at(reg):
    p0 = reg.create(id="todo", name="Todo")
    p1 = reg.update("todo", preview_url="http://x:8000", status="paused")
    assert p1.preview_url == "http://x:8000"
    assert p1.status == "paused"
    assert p1.name == "Todo"  # untouched
    assert p1.updated_at >= p0.updated_at


def test_update_unknown_raises(reg):
    with pytest.raises(KeyError):
        reg.update("nope", name="x")


def test_link_unlink_idempotent(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "g1")
    reg.link_goal("todo", "g1")  # idempotent
    assert reg.get("todo").goal_ids == ["g1"]
    reg.link_goal("todo", "g2")
    assert reg.get("todo").goal_ids == ["g1", "g2"]
    reg.unlink_goal("todo", "g1")
    assert reg.get("todo").goal_ids == ["g2"]
    reg.unlink_goal("todo", "absent")  # no-op
    assert reg.get("todo").goal_ids == ["g2"]


def test_delete(reg):
    reg.create(id="todo", name="Todo")
    assert reg.delete("todo") is True
    assert reg.get("todo") is None
    assert reg.delete("todo") is False  # already gone


def test_status_filter(reg):
    reg.create(id="a", name="A")
    reg.create(id="b", name="B")
    reg.update("b", status="archived")
    assert {p.id for p in reg.list(status="active")} == {"a"}
    assert {p.id for p in reg.list(status="archived")} == {"b"}


def test_persistence_across_reopen(tmp_path):
    db = str(tmp_path / "devclaw.db")
    ProjectRegistry(db).create(id="todo", name="Todo", goal_ids=["g1"])
    reopened = ProjectRegistry(db)
    p = reopened.get("todo")
    assert p is not None and p.goal_ids == ["g1"]


# ---- rollup + health -------------------------------------------------------


def _goal_get(table: dict):
    def get(gid: str) -> dict:
        if gid not in table:
            raise KeyError(gid)
        return table[gid]
    return get


def test_rollup_joins_live_goal_status(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "g1")
    table = {"g1": {"phase": "in_flight", "lifecycle": "executing",
                    "blocked_on": None, "progress": {"stalled": False},
                    "direction": {"verdict": "on_track"}}}
    out = project_rollup(reg.get("todo"), _goal_get(table))
    assert out["health"] == "working"
    assert out["goals"][0]["phase"] == "in_flight"
    assert out["goals"][0]["direction"]["verdict"] == "on_track"


def test_rollup_missing_goal_is_surfaced_not_dropped(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "ghost")
    out = project_rollup(reg.get("todo"), _goal_get({}))
    assert out["goals"] == [{"id": "ghost", "missing": True}]
    assert out["health"] == "idle"  # no live goals


def test_rollup_health_blocked_on_phase(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "g1")
    table = {"g1": {"phase": "blocked", "lifecycle": "executing", "progress": {}}}
    assert project_rollup(reg.get("todo"), _goal_get(table))["health"] == "blocked"


def test_rollup_health_blocked_on_stall(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "g1")
    table = {"g1": {"phase": "idle", "lifecycle": "executing", "progress": {"stalled": True}}}
    assert project_rollup(reg.get("todo"), _goal_get(table))["health"] == "blocked"


def test_rollup_health_done_when_all_done(reg):
    reg.create(id="todo", name="Todo")
    reg.link_goal("todo", "g1")
    reg.link_goal("todo", "g2")
    table = {
        "g1": {"phase": "done", "progress": {}},
        "g2": {"phase": "done", "progress": {}},
    }
    assert project_rollup(reg.get("todo"), _goal_get(table))["health"] == "done"


def test_rollup_health_archived_short_circuits(reg):
    reg.create(id="todo", name="Todo")
    reg.update("todo", status="archived")
    reg.link_goal("todo", "g1")
    table = {"g1": {"phase": "in_flight", "progress": {}}}
    assert project_rollup(reg.get("todo"), _goal_get(table))["health"] == "archived"


def test_busy_timeout_pragma_applied(reg):
    from devclaw.state_store import SQLITE_BUSY_TIMEOUT_MS

    got = reg._db.execute("PRAGMA busy_timeout").fetchone()[0]
    assert got == SQLITE_BUSY_TIMEOUT_MS
    assert got > 0  # a blocked writer waits, never fails fast at 0


def test_failed_create_does_not_leak_a_write_lock(tmp_path):
    """A duplicate create() raises ProjectExists — but it must ROLL BACK the failed
    INSERT's implicit transaction, not leave it open holding the write lock. The
    open-transaction leak was the root cause of the 75s `database is locked` stall:
    once one connection hit the duplicate-create path, it held the lock until its
    next commit, blocking every other connection's write."""
    db = str(tmp_path / "devclaw.db")
    a = ProjectRegistry(db)
    b = ProjectRegistry(db)
    a.create(id="dup", name="A")  # committed
    with pytest.raises(ProjectExists):
        a.create(id="dup", name="dup-again")  # IntegrityError -> must roll back

    # If `a` leaked the failed INSERT's transaction, it still holds the write lock
    # and this write on a *second* connection blocks until busy_timeout then raises.
    # A short timeout makes the test fail fast (instead of hanging) if the leak is back.
    b._db.execute("PRAGMA busy_timeout = 500")
    b.create(id="other", name="B")  # must succeed promptly — `a` holds no lock
    assert b.get("other") is not None


def test_contended_writer_waits_instead_of_failing(tmp_path):
    """Two connections to one db file (the CLI/server split). One holds the write
    lock; the other's write must WAIT for it (busy_timeout) and then succeed —
    not raise `database is locked` as it did before the timeout was set."""
    import threading
    import time

    db = str(tmp_path / "devclaw.db")
    holder = ProjectRegistry(db)
    writer = ProjectRegistry(db)

    holder._db.execute("BEGIN IMMEDIATE")  # grab + hold the single write lock

    released = threading.Event()

    def _release() -> None:
        time.sleep(0.3)  # << the writer's 5s busy_timeout, so it waits then wins
        holder._db.commit()
        released.set()

    t = threading.Thread(target=_release)
    t.start()
    writer.create(id="b", name="B")  # blocks until the holder commits, then writes
    t.join()

    assert released.is_set()
    assert writer.get("b") is not None
