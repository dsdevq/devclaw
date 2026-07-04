"""list_tasks — new filters for parent_goal_id / workspace_dir.

Pins the query surface the ProjectDetail "Recent tasks" and GoalDetail
"Dispatched tasks" sections consume. Parent_goal_id filter is exact match;
parent_goal_id_is_null combined with workspace_dir yields the "loose in this
project" set — no double-count against goal-owned tasks.
"""

from __future__ import annotations

import pytest

from devclaw.state_store import StateStore


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _t(store, tid, kind, workspace, goal_id):
    store.create_task(
        id=tid,
        kind=kind,
        workspace_dir=workspace,
        goal=f"task {tid}",
        parent_goal_id=goal_id,
    )


def test_filter_by_parent_goal_id_exact_match(store):
    _t(store, "t1", "implement_feature", "/ws1", "goal_a")
    _t(store, "t2", "fix_bug", "/ws1", "goal_a")
    _t(store, "t3", "implement_feature", "/ws1", "goal_b")
    _t(store, "t4", "implement_feature", "/ws1", None)

    got = store.list_tasks(parent_goal_id="goal_a")
    assert sorted(t.id for t in got) == ["t1", "t2"]


def test_filter_by_workspace_dir(store):
    _t(store, "t1", "implement_feature", "/ws1", None)
    _t(store, "t2", "fix_bug", "/ws2", None)
    _t(store, "t3", "implement_feature", "/ws1", "goal_a")

    got = store.list_tasks(workspace_dir="/ws1")
    assert sorted(t.id for t in got) == ["t1", "t3"]


def test_loose_in_project_set_via_null_and_workspace(store):
    """The ProjectDetail 'Recent tasks' section combines both filters."""
    _t(store, "t1", "implement_feature", "/ws1", None)  # loose, matches
    _t(store, "t2", "fix_bug", "/ws1", "goal_a")        # owned by goal, excluded
    _t(store, "t3", "implement_feature", "/ws2", None)  # wrong workspace
    _t(store, "t4", "fix_bug", "/ws1", None)            # loose, matches

    got = store.list_tasks(
        workspace_dir="/ws1", parent_goal_id_is_null=True
    )
    assert sorted(t.id for t in got) == ["t1", "t4"]


def test_ordering_is_newest_first(store):
    import time

    _t(store, "t1", "implement_feature", "/ws1", None)
    time.sleep(0.005)
    _t(store, "t2", "implement_feature", "/ws1", None)
    time.sleep(0.005)
    _t(store, "t3", "implement_feature", "/ws1", None)

    got = store.list_tasks(workspace_dir="/ws1")
    assert [t.id for t in got] == ["t3", "t2", "t1"]


def test_limit_caps_results(store):
    for i in range(5):
        _t(store, f"t{i}", "implement_feature", "/ws1", None)
    assert len(store.list_tasks(workspace_dir="/ws1", limit=3)) == 3
