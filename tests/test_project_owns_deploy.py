"""Escape-hatch pins for the container-per-goal → container-per-project flip.

When a target project's workspace contains a ``Dockerfile``, devclaw MUST NOT
spin its own ``devclaw-deploy-<goal_id>`` container on ``achieved`` — the
project's own CI (built by ``setup_cicd``) is the single deploy source. Without
this escape hatch, five closeloop goals produced five simultaneous closeloop
containers on the VPS (evidence, 2026-07-01). See
``~/memory/projects/devclaw/proposals/2026-07-01-per-project-runner-not-per-goal.md``.

The other side of the flip lives in ``setup_cicd``: pins here also cover the
scaffold — the Dockerfile it writes IS what future goals will observe.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from devclaw.goal.models import Goal
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import _auto_deploy, _project_owns_its_deploy
from devclaw.server.tools import _cicd_setup_sync


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
        suffix = await _auto_deploy(goal.id, goal, store)

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
        suffix = await _auto_deploy(goal.id, goal, store)

    mock_deploy.assert_called_once_with(str(tmp_path), goal.id)
    assert "https://vps.tail.ts.net:8090/" in suffix


async def test_auto_deploy_env_kill_switch_still_works(tmp_path, monkeypatch):
    """The DEVCLAW_GOAL_AUTODEPLOY=0 kill switch takes precedence over both
    branches — the pre-existing env-level opt-out is preserved."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")  # would-normally-skip
    monkeypatch.setenv("DEVCLAW_GOAL_AUTODEPLOY", "0")
    goal = _make_goal(str(tmp_path))
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / goal.id).mkdir()
    store = GoalStore(goals_dir)

    with patch("devclaw.goal.tick._deploy.deploy_project", new_callable=AsyncMock) as mock_deploy:
        suffix = await _auto_deploy(goal.id, goal, store)

    assert suffix == ""
    mock_deploy.assert_not_called()
    # And under env-off we don't log a "project owns" note either — the goal's
    # log stays clean of unrelated context.
    log_path = goals_dir / goal.id / "log.md"
    if log_path.exists():
        assert "project owns its deploy" not in log_path.read_text()


# --------------------------- setup_cicd scaffolds Dockerfile ---------------------------


def _init_git_repo(path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)


def test_setup_cicd_scaffolds_dockerfile_and_ci_for_node(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "package.json").write_text('{"name": "demo"}\n')

    # Skip the push step so we don't need a remote — patch _run to no-op after commit.
    import devclaw.server.tools as tools

    real_run = tools._run
    def fake_run(cmd, cwd):
        if cmd[:2] == ["git", "push"]:
            class R:
                returncode = 0
                stderr = ""
                stdout = ""
            return R()
        return real_run(cmd, cwd)

    with patch.object(tools, "_run", side_effect=fake_run):
        result = _cicd_setup_sync(str(tmp_path))

    assert result["status"] == "created"
    assert result["stack"] == "node"
    assert set(result["created"]) == {".github/workflows/ci.yml", "Dockerfile"}

    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "FROM node:" in dockerfile

    ci = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "runs-on: self-hosted" in ci
    assert "deploy:" in ci
    assert "docker build -t" in ci
    assert "APP_PORT" in ci


def test_setup_cicd_is_idempotent(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "package.json").write_text('{"name": "demo"}\n')
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "ci.yml").write_text("# already here\n")
    (tmp_path / "Dockerfile").write_text("FROM node:20\n")

    result = _cicd_setup_sync(str(tmp_path))
    assert result["status"] == "present"


def test_setup_cicd_creates_only_missing_artifact(tmp_path):
    """If the Dockerfile already exists but CI doesn't, scaffold just the CI —
    don't clobber the owner's tuned Dockerfile."""
    _init_git_repo(tmp_path)
    (tmp_path / "package.json").write_text('{"name": "demo"}\n')
    (tmp_path / "Dockerfile").write_text("# owner's tuned image\nFROM node:20-alpine AS custom\n")

    import devclaw.server.tools as tools
    real_run = tools._run
    def fake_run(cmd, cwd):
        if cmd[:2] == ["git", "push"]:
            class R:
                returncode = 0
                stderr = ""
                stdout = ""
            return R()
        return real_run(cmd, cwd)

    with patch.object(tools, "_run", side_effect=fake_run):
        result = _cicd_setup_sync(str(tmp_path))

    assert result["status"] == "updated"
    assert result["created"] == [".github/workflows/ci.yml"]
    # owner's Dockerfile untouched
    assert "owner's tuned image" in (tmp_path / "Dockerfile").read_text()


def test_setup_cicd_python_dockerfile(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

    import devclaw.server.tools as tools
    real_run = tools._run
    def fake_run(cmd, cwd):
        if cmd[:2] == ["git", "push"]:
            class R:
                returncode = 0
                stderr = ""
                stdout = ""
            return R()
        return real_run(cmd, cwd)

    with patch.object(tools, "_run", side_effect=fake_run):
        result = _cicd_setup_sync(str(tmp_path))

    assert result["stack"] == "python"
    assert "FROM python:" in (tmp_path / "Dockerfile").read_text()
