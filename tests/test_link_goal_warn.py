"""link_goal warn-first — one-goal-per-project (2026-07-04, warn phase).

Under the standing decision, a project pursues one well-defined goal at a
time. The warn phase surfaces the situation without rejecting the call, so a
week of production observability catches any legitimate cases we didn't
anticipate before the hard-reject flip.

Two entry points count toward the "already-has-active-goal" state:
  1. link_goal on the same project_id (advisory goal_ids list).
  2. create_goal on the same workspace_dir (authoritative join).

Both surface here.
"""

from __future__ import annotations

import json

import pytest

from devclaw.project_registry import ProjectRegistry
from devclaw.server import tools as _tools


@pytest.fixture()
def wire(monkeypatch, tmp_path):
    """Real registry + fake goal service so we can dial the goals list per test."""
    reg = ProjectRegistry(str(tmp_path / "reg.db"))
    fake_goals: list[dict] = []

    class _FakeGoals:
        def list_goals(self):
            return list(fake_goals)

    from devclaw.server import _state

    monkeypatch.setattr(_state, "registry", reg)
    monkeypatch.setattr(_state, "goals", _FakeGoals())
    # The tools module imports registry / goals at import time from _state, so
    # patch them there too.
    monkeypatch.setattr(_tools, "registry", reg)
    monkeypatch.setattr(_tools, "goals", _FakeGoals())
    yield reg, fake_goals
    reg.close() if hasattr(reg, "close") else None


async def test_first_link_carries_no_warning(wire):
    reg, fake_goals = wire
    reg.create(id="proj_a", name="A", workspace_dir="/ws")
    fake_goals.append({"id": "g1", "workspace_dir": "/ws", "phase": "executing"})

    raw = await _tools.link_goal("proj_a", "g1")
    out = json.loads(raw)
    assert "warning" not in out


async def test_second_active_link_returns_warning(wire):
    reg, fake_goals = wire
    reg.create(id="proj_a", name="A", workspace_dir="/ws")
    # goal 1 is already live in this workspace
    fake_goals.append({"id": "g1", "workspace_dir": "/ws", "phase": "executing"})
    reg.link_goal("proj_a", "g1")
    # now trying to link a second live goal
    fake_goals.append({"id": "g2", "workspace_dir": "/ws", "phase": "firming"})

    raw = await _tools.link_goal("proj_a", "g2")
    out = json.loads(raw)
    assert "warning" in out
    assert out["warning"]["code"] == "multiple_active_goals"
    assert out["warning"]["otherActiveGoalIds"] == ["g1"]
    # And the link still succeeded (warn, not reject).
    assert "g2" in out["goalIds"]


async def test_terminal_goal_does_not_count_as_active(wire):
    reg, fake_goals = wire
    reg.create(id="proj_a", name="A", workspace_dir="/ws")
    # goal 1 is done — should not block a new goal
    fake_goals.append({"id": "g1", "workspace_dir": "/ws", "phase": "achieved"})
    reg.link_goal("proj_a", "g1")
    fake_goals.append({"id": "g2", "workspace_dir": "/ws", "phase": "executing"})

    raw = await _tools.link_goal("proj_a", "g2")
    out = json.loads(raw)
    assert "warning" not in out


async def test_workspace_match_alone_counts(wire):
    """Even if only linked-by-workspace (never explicitly link_goal'd), an
    existing active goal blocks the linking of a second one."""
    reg, fake_goals = wire
    reg.create(id="proj_a", name="A", workspace_dir="/ws")
    # goal 1 is live via workspace match — NOT via link_goal
    fake_goals.append({"id": "g1", "workspace_dir": "/ws", "phase": "executing"})
    # trying to link a different goal id (still in the same workspace)
    fake_goals.append({"id": "g2", "workspace_dir": "/ws", "phase": "firming"})

    raw = await _tools.link_goal("proj_a", "g2")
    out = json.loads(raw)
    assert "warning" in out
    assert "g1" in out["warning"]["otherActiveGoalIds"]


async def test_unlink_never_warns(wire):
    reg, fake_goals = wire
    reg.create(id="proj_a", name="A", workspace_dir="/ws")
    reg.link_goal("proj_a", "g1")
    reg.link_goal("proj_a", "g2")
    fake_goals.append({"id": "g1", "workspace_dir": "/ws", "phase": "executing"})
    fake_goals.append({"id": "g2", "workspace_dir": "/ws", "phase": "executing"})

    raw = await _tools.link_goal("proj_a", "g2", unlink=True)
    out = json.loads(raw)
    assert "warning" not in out
