"""Regression test for the console task wire shape (ADR 0008 P1, PR-A).

The console's MILESTONE tier is a *view*: it groups a goal's tasks by their
existing ``plan_key`` (no new table). That grouping only works if the wire the
console eats actually carries ``planKey`` — so ``_task_row`` must surface both
``plan_key`` and ``milestone`` off the persisted Task row. These pin that: a
task persisted with a plan_key/milestone shows them on the wire; a standalone
task shows them as null (never absent, never crashing the grouping view).
"""

from __future__ import annotations

import pytest

from devclaw.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


def test_task_row_surfaces_plan_key_and_milestone_for_grouping(store):
    from devclaw.server.http import _task_row

    store.create_task(
        id="t-grouped",
        kind="implement_feature",
        workspace_dir="/w",
        goal="build the thing",
        parent_goal_id="g1",
        milestone="M2 — API layer",
        plan_key="checklist-item-7",
    )
    row = _task_row(store.get_task("t-grouped"))

    assert row["planKey"] == "checklist-item-7"
    assert row["milestone"] == "M2 — API layer"


def test_task_row_plan_key_and_milestone_null_for_standalone_task(store):
    from devclaw.server.http import _task_row

    store.create_task(
        id="t-loose",
        kind="fix_bug",
        workspace_dir="/w",
        goal="one-off fix",
    )
    row = _task_row(store.get_task("t-loose"))

    # Both keys are always present on the wire (the grouping view keys off them);
    # a standalone task simply has them null rather than omitted.
    assert row["planKey"] is None
    assert row["milestone"] is None
