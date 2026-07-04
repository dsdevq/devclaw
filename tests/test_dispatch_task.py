"""dispatch_task — the consolidated one-shot task tool.

Pins:
  1. Each kind (implement_feature, fix_bug, review_repository) reaches
     queue.submit with the correct ``kind`` string, so downstream routing
     (planner _VALID_TOOLS, claude_sdk._PROMPT_SLUGS, engine.py review branch,
     state_store.TaskKind) keeps working unchanged.
  2. review_repository ignores verify_cmd + open_pr — the old dedicated tool
     never accepted them, and the merge must not smuggle a gate/PR into a
     read-only review.
  3. The deprecated aliases (implement_feature, fix_bug, review_repository)
     still call queue.submit with the same kind/goal/deliver as before, so
     external MCP callers don't break.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from devclaw.server import tools as _tools


@pytest.fixture
def capture_submit(monkeypatch):
    """Replace queue.submit with a spy; return a list that captures each call."""
    calls: list[dict] = []

    def _fake_submit(**kwargs) -> str:
        calls.append(kwargs)
        return f"task_{len(calls)}"

    from devclaw.server import _state

    monkeypatch.setattr(_state.queue, "submit", _fake_submit)
    return calls


async def test_dispatch_task_implement_feature_forwards_kind_and_deliver(capture_submit):
    raw = await _tools.dispatch_task(
        kind="implement_feature",
        workspace_dir="/tmp/wsp",
        goal="add /health",
        verify_cmd="pytest -q",
        open_pr=True,
    )
    result = json.loads(raw)
    assert result["task_id"] == "task_1"
    assert result["status"] == "pending"
    (call,) = capture_submit
    assert call["kind"] == "implement_feature"
    assert call["workspace_dir"] == "/tmp/wsp"
    assert call["goal"] == "add /health"
    assert call["verify_cmd"] == "pytest -q"
    assert call["deliver"] is True


async def test_dispatch_task_fix_bug_forwards_kind(capture_submit):
    await _tools.dispatch_task(
        kind="fix_bug",
        workspace_dir="/tmp/wsp",
        goal="fix crash on empty payload",
        verify_cmd="pytest",
    )
    (call,) = capture_submit
    assert call["kind"] == "fix_bug"
    assert call["goal"] == "fix crash on empty payload"
    assert call["verify_cmd"] == "pytest"
    assert call["deliver"] is False


async def test_dispatch_task_review_repository_ignores_verify_and_open_pr(capture_submit):
    await _tools.dispatch_task(
        kind="review_repository",
        workspace_dir="/tmp/wsp",
        goal="focus on auth",
        verify_cmd="pytest",
        open_pr=True,
    )
    (call,) = capture_submit
    assert call["kind"] == "review_repository"
    assert call["verify_cmd"] is None, "review is read-only — no verify gate"
    assert call["deliver"] is False, "review is read-only — no PR delivery"


async def test_dispatch_task_rejects_empty_workspace_or_goal():
    with pytest.raises(ToolError, match="workspace_dir and goal"):
        await _tools.dispatch_task(
            kind="implement_feature", workspace_dir="", goal="x"
        )
    with pytest.raises(ToolError, match="workspace_dir and goal"):
        await _tools.dispatch_task(
            kind="implement_feature", workspace_dir="/tmp/wsp", goal=""
        )


async def test_implement_feature_alias_still_submits_same_kind(capture_submit):
    await _tools.implement_feature(
        workspace_dir="/tmp/wsp", goal="add /health", open_pr=True
    )
    (call,) = capture_submit
    assert call["kind"] == "implement_feature"
    assert call["deliver"] is True


async def test_fix_bug_alias_still_submits_same_kind(capture_submit):
    await _tools.fix_bug(
        workspace_dir="/tmp/wsp", description="crash on empty payload"
    )
    (call,) = capture_submit
    assert call["kind"] == "fix_bug"
    assert call["goal"] == "crash on empty payload"


async def test_review_repository_alias_still_submits_same_kind(capture_submit):
    await _tools.review_repository(workspace_dir="/tmp/wsp", focus="auth")
    (call,) = capture_submit
    assert call["kind"] == "review_repository"
    assert call["goal"] == "auth"
    assert call["verify_cmd"] is None
    assert call["deliver"] is False


async def test_review_repository_alias_defaults_goal_when_no_focus(capture_submit):
    await _tools.review_repository(workspace_dir="/tmp/wsp")
    (call,) = capture_submit
    assert call["goal"] == "general code review"
