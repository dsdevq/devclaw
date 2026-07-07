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
#
# The rollup joins project↔goals by workspace_dir match, NOT by a stored
# goal_ids list (retained as advisory only). Tests below build the input the
# rollup actually gets — a full goals list — and assert the workspace match
# is what drives association.


def _goal(id: str, workspace_dir: str, **fields) -> dict:
    """Build a goals-list entry (goal_service.list_goals shape) for tests."""
    base = {"id": id, "workspace_dir": workspace_dir}
    base.update(fields)
    return base


def test_rollup_joins_by_workspace_dir(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    all_goals = [
        _goal("g1", "/src/todo", phase="in_flight", lifecycle="executing",
              blocked_on=None, progress={"stalled": False},
              direction={"verdict": "on_track"}),
        _goal("g-other", "/src/somewhere-else", phase="in_flight",
              progress={"stalled": False}),
    ]
    out = project_rollup(reg.get("todo"), all_goals)
    assert out["health"] == "working"
    assert len(out["goals"]) == 1
    assert out["goals"][0]["id"] == "g1"
    assert out["goals"][0]["direction"]["verdict"] == "on_track"


def test_rollup_normalizes_workspace_paths(reg):
    """A trailing slash / double slash on either side of the join must not
    hide a matching goal — projects and goals may set the workspace_dir via
    different code paths that don't agree on formatting."""
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo/")
    all_goals = [
        _goal("g1", "/src//todo", phase="in_flight", progress={"stalled": False}),
    ]
    out = project_rollup(reg.get("todo"), all_goals)
    assert len(out["goals"]) == 1 and out["goals"][0]["id"] == "g1"


def test_rollup_ignores_stored_goal_ids(reg):
    """Explicit link_goal calls do NOT bring an unrelated-workspace goal
    into the rollup. This is the guard for the cancel-and-refile drift the
    workspace-match design was introduced to eliminate."""
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    reg.link_goal("todo", "some-old-goal")  # advisory; must not affect rollup
    all_goals = [
        _goal("some-old-goal", "/src/other", phase="in_flight",
              progress={"stalled": False}),
    ]
    out = project_rollup(reg.get("todo"), all_goals)
    assert out["goals"] == []
    assert out["health"] == "idle"


def test_rollup_no_workspace_dir_yields_no_goals(reg):
    reg.create(id="todo", name="Todo")  # no workspace_dir
    all_goals = [_goal("g1", "/anything", phase="in_flight", progress={})]
    out = project_rollup(reg.get("todo"), all_goals)
    assert out["goals"] == [] and out["health"] == "idle"


def test_rollup_health_blocked_on_phase(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    all_goals = [_goal("g1", "/src/todo", phase="blocked",
                       lifecycle="executing", progress={})]
    assert project_rollup(reg.get("todo"), all_goals)["health"] == "blocked"


def test_rollup_health_blocked_on_stall(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    all_goals = [_goal("g1", "/src/todo", phase="idle",
                       lifecycle="executing", progress={"stalled": True})]
    assert project_rollup(reg.get("todo"), all_goals)["health"] == "blocked"


def test_rollup_health_done_when_all_done(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    all_goals = [
        _goal("g1", "/src/todo", phase="done", progress={}),
        _goal("g2", "/src/todo", phase="done", progress={}),
    ]
    assert project_rollup(reg.get("todo"), all_goals)["health"] == "done"


def test_rollup_health_archived_short_circuits(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo")
    reg.update("todo", status="archived")
    all_goals = [_goal("g1", "/src/todo", phase="in_flight", progress={})]
    assert project_rollup(reg.get("todo"), all_goals)["health"] == "archived"


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


# ---- automerge: per-project override -----------------------------------
#
# Deliberately NOT a goal.yaml field (see devclaw.goal.merge) — the only place
# auto-merge is configured is here: a project's own override, or nothing
# (meaning "inherit the devclaw-wide default").


def test_automerge_defaults_to_none_on_create(reg):
    p = reg.create(id="todo", name="Todo")
    assert p.automerge is None
    assert reg.get("todo").automerge is None


def test_automerge_set_on_create(reg):
    on = reg.create(id="on", name="On", automerge=True)
    off = reg.create(id="off", name="Off", automerge=False)
    assert reg.get("on").automerge is True
    assert reg.get("off").automerge is False


def test_update_omitting_automerge_leaves_it_untouched(reg):
    reg.create(id="todo", name="Todo", automerge=True)
    reg.update("todo", notes="unrelated change")
    assert reg.get("todo").automerge is True


def test_update_can_set_automerge_on_or_off(reg):
    reg.create(id="todo", name="Todo")
    reg.update("todo", automerge=True)
    assert reg.get("todo").automerge is True
    reg.update("todo", automerge=False)
    assert reg.get("todo").automerge is False


def test_update_explicit_none_clears_automerge_override(reg):
    """Passing automerge=None explicitly is different from omitting it — it
    clears a prior pin back to 'inherit the global default'."""
    reg.create(id="todo", name="Todo", automerge=True)
    reg.update("todo", automerge=None)
    assert reg.get("todo").automerge is None


def test_automerge_persists_across_reopen(tmp_path):
    db = str(tmp_path / "devclaw.db")
    ProjectRegistry(db).create(id="todo", name="Todo", automerge=True)
    reopened = ProjectRegistry(db)
    assert reopened.get("todo").automerge is True


def test_automerge_column_migrates_onto_a_pre_existing_table(tmp_path):
    """A projects table created before the automerge column existed must gain
    it on the next open, not error and not lose existing rows."""
    import sqlite3

    db = str(tmp_path / "devclaw.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, repo_url TEXT,
          workspace_dir TEXT, preview_url TEXT, status TEXT NOT NULL DEFAULT 'active',
          goal_ids TEXT, notes TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        """
    )
    con.execute(
        "INSERT INTO projects (id, name, status, created_at, updated_at) "
        "VALUES ('legacy', 'Legacy', 'active', 0, 0)"
    )
    con.commit()
    con.close()

    reg = ProjectRegistry(db)  # must not raise
    p = reg.get("legacy")
    assert p is not None and p.automerge is None  # pre-existing row reads as "inherit"
    reg.update("legacy", automerge=True)  # column is genuinely writable now
    assert reg.get("legacy").automerge is True


def test_find_by_workspace_dir(reg):
    reg.create(id="todo", name="Todo", workspace_dir="/src/todo/", automerge=True)
    found = reg.find_by_workspace_dir("/src//todo")  # normalized match
    assert found is not None and found.id == "todo"
    assert reg.find_by_workspace_dir("/src/nope") is None
    assert reg.find_by_workspace_dir(None) is None
    assert reg.find_by_workspace_dir("") is None


def test_automerge_in_to_dict(reg):
    p = reg.create(id="todo", name="Todo", automerge=False)
    assert p.to_dict()["automerge"] is False


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
