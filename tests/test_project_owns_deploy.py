"""Escape-hatch pins for the container-per-goal → container-per-project flip.

When a target project's workspace contains a ``Dockerfile``, devclaw MUST NOT
spin its own ``devclaw-deploy-<goal_id>`` container on ``achieved`` — the
project's own CI is the single deploy source. Without this escape hatch, five
closeloop goals produced five simultaneous closeloop containers on the VPS
(evidence, 2026-07-01). See
``~/memory/projects/devclaw/proposals/2026-07-01-per-project-runner-not-per-goal.md``.

The escape-hatch signal (Dockerfile at workspace root) is pure mechanism and is
what gets pinned here. How that Dockerfile gets there is engineering-judgment
work an ``implement_feature`` task does per-project — devclaw does NOT ship a
template scaffolder for it (the earlier ``setup_cicd`` MCP tool was removed
after it hardcoded five stack templates and silently misdetected fullstack
apps; per ``plan.md`` §Production-ready criterion C5, CI/Dockerfile shape is
per-project standards work that stays with the code, not universal mechanism).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from devclaw.goal.models import Goal
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import _auto_deploy, _project_owns_its_deploy


def _make_goal(workspace_dir: str) -> Goal:
    return Goal(
        id="demo-goal",
        objective="ship it",
        cadence="1d",
        engine="devclaw",
        workspace_dir=workspace_dir,
        repo_url=None,
        verify_cmd=None,
        open_pr=True,
        done_when="deployed",
    )


# --------------------------- _project_owns_its_deploy ---------------------------


def test_owns_deploy_returns_true_when_dockerfile_exists(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    assert _project_owns_its_deploy(str(tmp_path)) is True


def test_owns_deploy_returns_false_when_no_dockerfile(tmp_path):
    assert _project_owns_its_deploy(str(tmp_path)) is False


def test_owns_deploy_returns_false_on_missing_workspace():
    assert _project_owns_its_deploy("/nonexistent/path") is False


# --------------------------- _auto_deploy escape hatch ---------------------------


async def test_auto_deploy_skips_when_dockerfile_present(tmp_path):
    """The load-bearing pin: with Dockerfile present, devclaw MUST NOT call
    deploy_project — the whole reason for this flip."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    goal = _make_goal(str(tmp_path))
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / goal.id).mkdir()
    store = GoalStore(goals_dir)

    with patch("devclaw.goal.tick._deploy.deploy_project", new_callable=AsyncMock) as mock_deploy:
        suffix = await _auto_deploy(goal.id, goal, store, enabled=True)

    assert suffix == ""
    mock_deploy.assert_not_called()
    log = (goals_dir / goal.id / "log.md").read_text()
    assert "project owns its deploy" in log
    assert "Dockerfile present" in log


async def test_auto_deploy_still_fires_without_dockerfile(tmp_path):
    """Backward-compat pin: projects that haven't migrated (no Dockerfile) still
    get the old auto-deploy — no regression during the migration window."""
    goal = _make_goal(str(tmp_path))
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / goal.id).mkdir()
    store = GoalStore(goals_dir)

    fake_result = {
        "url": "https://vps.tail.ts.net:8090/",
        "container": "devclaw-deploy-demo-goal",
        "ready": True,
        "tailscale_served": True,
    }

    with patch(
        "devclaw.goal.tick._deploy.deploy_project", new_callable=AsyncMock
    ) as mock_deploy:
        mock_deploy.return_value = fake_result
        suffix = await _auto_deploy(goal.id, goal, store, enabled=True)

    mock_deploy.assert_called_once_with(str(tmp_path), goal.id)
    assert "https://vps.tail.ts.net:8090/" in suffix


async def test_auto_deploy_kill_switch_still_works(tmp_path):
    """The autodeploy kill switch (now the resolved ``enabled=False`` flag —
    a project override or the DEVCLAW_GOAL_AUTODEPLOY default, resolved upstream
    in GoalService) takes precedence over both branches: disabled means no
    deploy, no matter the Dockerfile."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")  # would-normally-skip
    goal = _make_goal(str(tmp_path))
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / goal.id).mkdir()
    store = GoalStore(goals_dir)

    with patch("devclaw.goal.tick._deploy.deploy_project", new_callable=AsyncMock) as mock_deploy:
        suffix = await _auto_deploy(goal.id, goal, store, enabled=False)

    assert suffix == ""
    mock_deploy.assert_not_called()
    # And under env-off we don't log a "project owns" note either — the goal's
    # log stays clean of unrelated context.
    log_path = goals_dir / goal.id / "log.md"
    if log_path.exists():
        assert "project owns its deploy" not in log_path.read_text()
