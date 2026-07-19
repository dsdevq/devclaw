"""Warn on bare verify_cmd at create_goal time. (Warnings now flow through
the structured admission framework; rejection-class admission tests live in
``test_goal_admission.py``.)"""

from __future__ import annotations

import pytest

from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue

#: Minimal admittable goal scaffolding. Every test in this file is exercising
#: WARNING behavior (bare verify_cmd), so we hand each create_goal call a
#: done_when that's substantive enough to pass admission and a backlog so the
#: from-scratch anchor check passes too. Adjust here, not per-test, when
#: admission requirements widen.
_OK_DONE_WHEN = "the test command exits 0 and at least one assertion runs."
_OK_BACKLOG = ["scaffold project", "wire the verify_cmd"]


def _ok(svc, goal_id: str, **overrides):
    """Create a minimally-admittable goal, applying caller overrides."""
    kw = dict(
        objective="ship it",
        workspace_dir="/ws",
        done_when=_OK_DONE_WHEN,
        backlog=_OK_BACKLOG,
    )
    kw.update(overrides)
    return svc.create_goal(goal_id, **kw)


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
    result = _ok(svc, "g-bare", verify_cmd="pytest")
    assert "warnings" in result
    assert len(result["warnings"]) == 1
    w = result["warnings"][0]
    assert "pytest" in w
    assert "PATH" in w


def test_python_m_pytest_returns_no_warning(svc):
    result = _ok(svc, "g-ok", verify_cmd="python -m pytest")
    assert result.get("warnings", []) == []


def test_no_verify_cmd_returns_no_warning(svc):
    result = _ok(svc, "g-none")
    assert result.get("warnings", []) == []


def test_full_path_cmd_returns_no_warning(svc):
    result = _ok(svc, "g-full", verify_cmd="/usr/bin/pytest")
    assert result.get("warnings", []) == []


def test_dotnet_test_returns_no_warning(svc):
    result = _ok(svc, "g-dotnet", verify_cmd="dotnet test")
    assert result.get("warnings", []) == []


@pytest.mark.parametrize("cmd", ["pytest", "python", "node", "npm"])
def test_common_bare_tools_all_warn(svc, cmd):
    result = _ok(svc, f"g-{cmd}", verify_cmd=cmd)
    assert "warnings" in result, f"expected warning for verify_cmd={cmd!r}"
    assert cmd in result["warnings"][0]


def test_spec_param_is_persisted(svc):
    """When the OpenClaw waiter has grilled scope before filing the order, it
    passes the finalized spec via create_goal — the service persists it so the
    evaluator can judge done against the shared contract."""
    spec_text = "# my-app — spec\n## Goal\nA tiny CLI.\n## Scope\nin: foo\nout: bar"
    # spec alone is enough to admit (no done_when needed; spec carries it).
    svc.create_goal("g-spec", objective="ship cli", workspace_dir="/ws", spec=spec_text)
    persisted = svc._goal_store.read_spec("g-spec")
    assert "Goal" in persisted and "A tiny CLI." in persisted


def test_no_spec_param_writes_nothing(svc):
    _ok(svc, "g-nospec")
    assert svc._goal_store.read_spec("g-nospec") == ""


def test_create_goal_rejects_unknown_mode(svc):
    """ADR 0003 stage 2: the execution dial accepts exactly long_lived |
    one_shot — a typo'd mode must fail creation loudly, never write a
    goal.yaml that silently runs the wrong loop."""
    with pytest.raises(ValueError, match="unknown goal mode"):
        _ok(svc, "g-mode", mode="oneshot")


def test_create_goal_persists_one_shot_mode(svc):
    _ok(svc, "g-os", mode="one_shot")
    assert svc._goal_store.load_goal("g-os").mode == "one_shot"
