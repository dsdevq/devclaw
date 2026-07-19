"""MCP ``start_program`` as sugar for ``create_goal(mode='one_shot')`` —
ADR 0003 stage 2b (the surface collapse).

The tool no longer submits a raw queue program: it files a ONE-SHOT GOAL —
same intake spine, checklist-as-one-parallel-program execution, PR-per-slice
delivery, grounded done-gate close — and returns the goal_id. The brief rides
as the goal's SPEC (the scope contract firming derives done_when from), which
is what keeps admission parity with the old direct-queue path: a substantial
brief plans; nothing new is demanded of the caller.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal.service import GoalConfig, GoalService
from devclaw.server import tools as _tools
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


_BRIEF = (
    "Build the accounts screen: list accounts from the API, add a detail "
    "panel, and cover both with integration tests."
)


@pytest.fixture
def svc(tmp_path):
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store)
    cfg = GoalConfig(
        goals_dir=tmp_path / "goals", notify_url="",
        tick_seconds=900, eval_every=5, verify_done=False,
    )
    svc = GoalService(queue, store, cfg)
    yield svc
    store.close()


@pytest.fixture(autouse=True)
def _patch(svc, monkeypatch):
    monkeypatch.setattr(_tools, "goals", svc)
    return svc


async def test_start_program_files_a_one_shot_goal_not_a_queue_program(svc):
    out = json.loads(await _tools.start_program(workspace_dir="/ws", goal=_BRIEF))

    assert out["mode"] == "one_shot"
    goal_id = out["goal_id"]
    g = svc._goal_store.load_goal(goal_id)
    assert g.mode == "one_shot"
    assert g.objective == _BRIEF
    # NO raw program was submitted — the goal's own tick dispatches the child
    # program later, with the goal as parent.
    assert svc._store.list_programs() == []
    # the response tells the caller how to follow it
    assert "get_goal" in out["note"]


async def test_start_program_brief_rides_as_the_spec(svc):
    out = json.loads(await _tools.start_program(workspace_dir="/ws", goal=_BRIEF))
    spec = svc._goal_store.read_spec(out["goal_id"])
    assert spec is not None and _BRIEF in spec


async def test_start_program_notify_url_is_flagged_not_silently_dropped(svc):
    out = json.loads(await _tools.start_program(
        workspace_dir="/ws", goal=_BRIEF, notify_url="https://hook.example/x",
    ))
    assert "notify_url_ignored" in out


async def test_start_program_still_requires_workspace_and_goal(svc):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await _tools.start_program(workspace_dir="", goal=_BRIEF)
    with pytest.raises(ToolError):
        await _tools.start_program(workspace_dir="/ws", goal="")


async def test_start_program_goal_ids_are_readable_and_unique(svc):
    a = json.loads(await _tools.start_program(workspace_dir="/ws", goal=_BRIEF))
    b = json.loads(await _tools.start_program(workspace_dir="/ws", goal=_BRIEF))
    assert a["goal_id"] != b["goal_id"]          # uuid suffix — no collision
    assert a["goal_id"].startswith("build-the-accounts-screen")
