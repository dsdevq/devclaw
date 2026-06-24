"""Warn on bare verify_cmd at create_goal time."""

from __future__ import annotations

import pytest

from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def svc(tmp_path):
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store)
    cfg = GoalConfig(
        goals_dir=tmp_path / "goals",
        notify_url="",
        tick_seconds=900,
        eval_every=5,
        verify_done=False,
    )
    svc = GoalService(queue, store, cfg)
    yield svc
    store.close()


def test_bare_tool_name_returns_warning(svc):
    result = svc.create_goal(
        "g-bare", objective="ship it", workspace_dir="/ws",
        verify_cmd="pytest",
    )
    assert "warnings" in result
    assert len(result["warnings"]) == 1
    w = result["warnings"][0]
    assert "pytest" in w
    assert "PATH" in w


def test_python_m_pytest_returns_no_warning(svc):
    result = svc.create_goal(
        "g-ok", objective="ship it too", workspace_dir="/ws",
        verify_cmd="python -m pytest",
    )
    assert result.get("warnings", []) == []


def test_no_verify_cmd_returns_no_warning(svc):
    result = svc.create_goal(
        "g-none", objective="no gate", workspace_dir="/ws",
    )
    assert result.get("warnings", []) == []


def test_full_path_cmd_returns_no_warning(svc):
    result = svc.create_goal(
        "g-full", objective="full path", workspace_dir="/ws",
        verify_cmd="/usr/bin/pytest",
    )
    assert result.get("warnings", []) == []


def test_dotnet_test_returns_no_warning(svc):
    result = svc.create_goal(
        "g-dotnet", objective="dotnet test", workspace_dir="/ws",
        verify_cmd="dotnet test",
    )
    assert result.get("warnings", []) == []


@pytest.mark.parametrize("cmd", ["pytest", "python", "node", "npm"])
def test_common_bare_tools_all_warn(svc, cmd):
    result = svc.create_goal(
        f"g-{cmd}", objective=f"run {cmd}", workspace_dir="/ws",
        verify_cmd=cmd,
    )
    assert "warnings" in result, f"expected warning for verify_cmd={cmd!r}"
    assert cmd in result["warnings"][0]
