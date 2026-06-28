"""Skill-loader + hook-runner tests for the in-sandbox runner.

The runner concatenates markdown files baked into the sandbox image at
/opt/devclaw/skills/. Tests point `_SKILLS_DIR` at the in-repo source so the
loader is exercised against the same files that get baked.
"""

import importlib.util
import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _REPO_ROOT / "openhands-runner" / "runner.py"
_SKILLS_SRC = _REPO_ROOT / "openhands-runner" / "skills"
_HOOKS_SRC = _REPO_ROOT / "openhands-runner" / "hooks"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_skills_under_test", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def skill_dir(runner, monkeypatch):
    monkeypatch.setattr(runner, "_SKILLS_DIR", str(_SKILLS_SRC))
    return _SKILLS_SRC


@pytest.fixture
def hook_dir(runner, monkeypatch):
    monkeypatch.setattr(runner, "_HOOKS_DIR", str(_HOOKS_SRC))
    return _HOOKS_SRC


# ---- _load_skills behavior --------------------------------------------------


def test_common_skill_loads_for_every_kind(runner, skill_dir):
    for kind in ("implement_feature", "fix_bug", "review_repository", "onboard"):
        bundle = runner._load_skills(kind)
        assert "Common operating context" in bundle
        assert "AGENTS.md" in bundle


def test_writes_code_tier_loads_for_code_writing_kinds_only(runner, skill_dir):
    for kind in ("implement_feature", "fix_bug"):
        bundle = runner._load_skills(kind)
        assert "Quality bar" in bundle
        assert "Verify-gate coverage" in bundle
        assert "Playwright" in bundle  # e2e skill
        assert "Commit hygiene" in bundle
    for kind in ("review_repository", "onboard"):
        bundle = runner._load_skills(kind)
        assert "Quality bar" not in bundle
        assert "Commit hygiene" not in bundle


def test_fix_bug_keeps_its_smallest_change_skill(runner, skill_dir):
    bundle = runner._load_skills("fix_bug")
    assert "smallest change" in bundle.lower()


def test_review_repository_loads_only_read_only_skill(runner, skill_dir):
    bundle = runner._load_skills("review_repository")
    assert "READ ONLY" in bundle
    assert "Commit hygiene" not in bundle  # no code-writing tier


def test_onboard_loads_agents_md_doctrine(runner, skill_dir):
    bundle = runner._load_skills("onboard")
    assert "ONBOARDING" in bundle
    assert "AGENTS.md" in bundle


def test_skill_blocks_are_separated_by_horizontal_rule(runner, skill_dir):
    bundle = runner._load_skills("implement_feature")
    # the loader joins with "\n\n---\n\n" so each skill is clearly delimited
    assert "\n\n---\n\n" in bundle


# ---- _wrap_goal integration -------------------------------------------------


def test_wrap_goal_uses_skills_when_dir_present(runner, skill_dir):
    wrapped = runner._wrap_goal("implement_feature", "GOAL-TOKEN")
    assert "Common operating context" in wrapped
    assert "## Goal" in wrapped
    assert wrapped.rstrip().endswith("GOAL-TOKEN")


def test_wrap_goal_falls_back_when_skill_dir_missing(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_SKILLS_DIR", str(tmp_path / "nonexistent"))
    wrapped = runner._wrap_goal("implement_feature", "GOAL-TOKEN")
    # Legacy embedded preamble still works in degraded mode (host-side dev).
    assert "GOAL-TOKEN" in wrapped
    assert wrapped != "GOAL-TOKEN"
    assert "AGENTS.md" in wrapped  # from embedded _CONTEXT_PREAMBLE


# ---- _run_hook behavior -----------------------------------------------------


def test_run_hook_returns_false_when_missing(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_HOOKS_DIR", str(tmp_path / "nonexistent"))
    ran, out = runner._run_hook("pre-run", "/tmp", "implement_feature", "task-id")
    assert ran is False
    assert out == ""


def test_pre_run_hook_executes(runner, hook_dir, tmp_path):
    # workspace must exist and look like a git repo for pre-run to snapshot HEAD
    ws = tmp_path / "ws"
    ws.mkdir()
    ran, out = runner._run_hook("pre-run", str(ws), "implement_feature", "task-id")
    assert ran is True
    # workspace is not a git repo so no snapshot file created — no warnings
    # expected on this happy path; output may be empty.
    assert "fatal" not in out.lower()


def test_post_run_hook_warns_on_e2e_without_playwright_in_verify(runner, hook_dir, tmp_path):
    # Simulate: pre-run snapshotted a HEAD, agent added an e2e spec, verify_cmd
    # is pytest-only. Post-run should warn.
    import subprocess as sp
    ws = tmp_path / "ws"
    ws.mkdir()
    sp.run(["git", "init", "-q", str(ws)], check=True)
    sp.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    (ws / "README.md").write_text("x")
    sp.run(["git", "-C", str(ws), "add", "."], check=True)
    sp.run(["git", "-C", str(ws), "commit", "-q", "-m", "init"], check=True)
    pre_head = sp.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (ws / ".devclaw-pre-head").write_text(pre_head)
    # Agent shipped a spec file
    (ws / "e2e").mkdir()
    (ws / "e2e" / "smoke.spec.ts").write_text("test('x', () => {});")
    sp.run(["git", "-C", str(ws), "add", "."], check=True)
    sp.run(["git", "-C", str(ws), "commit", "-q", "-m", "add e2e"], check=True)

    ran, out = runner._run_hook(
        "post-run", str(ws), "implement_feature", "task-id", "pytest -q"
    )
    assert ran is True
    assert "browser tests added but verify_cmd" in out
    assert "smoke.spec.ts" in out
