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


def test_fix_bug_loads_diagnosis_loop_after_scope(runner, skill_dir):
    """The diagnosis-loop discipline ships in the fix_bug tier, ordered after
    the scope skill (00- prefix before 10-) so scope framing comes first."""
    bundle = runner._load_skills("fix_bug")
    assert "Diagnosis loop" in bundle
    assert "red-capable" in bundle
    assert bundle.index("Bug-fix scope") < bundle.index("Diagnosis loop")
    for kind in ("implement_feature", "review_repository", "onboard"):
        assert "Diagnosis loop" not in runner._load_skills(kind)


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


# ---- per-repo .agent/skills/ loading (D11) ----------------------------------


def test_per_repo_skill_appended_when_workspace_provided(runner, skill_dir, tmp_path):
    """A repo can carry observations the universal skills can't (e.g.
    "App.tsx is a 1827-line monolith") in <workspace>/.agent/skills/. The
    worker should see both the universal doctrine AND the per-repo notes."""
    repo_skills = tmp_path / ".agent" / "skills"
    repo_skills.mkdir(parents=True)
    (repo_skills / "frontend-structure.md").write_text(
        "# Frontend structure\n\nApp.tsx is a known 1827-line monolith.\n",
        encoding="utf-8",
    )
    bundle = runner._load_skills("implement_feature", workspace_dir=str(tmp_path))
    # universal skill present
    assert "Quality bar" in bundle
    # per-repo skill present too
    assert "1827-line monolith" in bundle


def test_per_repo_skill_loads_writes_code_tier(runner, skill_dir, tmp_path):
    """Per-repo _writes-code/ skills should load for code-writing kinds, matching
    the universal layout so a repo can add its own per-kind overrides."""
    repo_writes = tmp_path / ".agent" / "skills" / "_writes-code"
    repo_writes.mkdir(parents=True)
    (repo_writes / "20-repo-rule.md").write_text(
        "# Repo-specific rule\n\nNever import lodash.\n",
        encoding="utf-8",
    )
    for kind in ("implement_feature", "fix_bug"):
        bundle = runner._load_skills(kind, workspace_dir=str(tmp_path))
        assert "Never import lodash" in bundle
    # read-only kinds skip the writes-code tier per the existing rule
    bundle = runner._load_skills("review_repository", workspace_dir=str(tmp_path))
    assert "Never import lodash" not in bundle


def test_per_repo_skill_universal_comes_first(runner, skill_dir, tmp_path):
    """Universal devclaw doctrine appears BEFORE per-repo notes — the repo
    leans on what the agent already knows, not the other way around."""
    repo_skills = tmp_path / ".agent" / "skills"
    repo_skills.mkdir(parents=True)
    (repo_skills / "_common.md").write_text(
        "# Per-repo common\n\nREPO-COMMON-MARKER\n", encoding="utf-8",
    )
    bundle = runner._load_skills("implement_feature", workspace_dir=str(tmp_path))
    assert bundle.index("Common operating context") < bundle.index("REPO-COMMON-MARKER")


def test_per_repo_skill_missing_dir_is_silent(runner, skill_dir, tmp_path):
    """A workspace with no .agent/skills/ must not crash; loader returns
    just the universal bundle."""
    bundle = runner._load_skills("implement_feature", workspace_dir=str(tmp_path))
    assert "Quality bar" in bundle  # universal still there


def test_load_skills_default_arg_keeps_legacy_behavior(runner, skill_dir):
    """The workspace_dir kwarg defaults to None so the loader stays
    backward-compatible — universal-only bundle, unchanged."""
    bundle = runner._load_skills("implement_feature")
    assert "Quality bar" in bundle


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


def test_wrap_goal_threads_workspace_dir_to_per_repo_skills(runner, skill_dir, tmp_path):
    """The full integration path: a repo carrying .agent/skills/ + a wrapped
    goal must produce a prompt containing BOTH the universal skill and the
    per-repo observation — proving D11 is wired end-to-end through _wrap_goal."""
    repo_skills = tmp_path / ".agent" / "skills"
    repo_skills.mkdir(parents=True)
    (repo_skills / "structure.md").write_text(
        "# Repo structure\n\nREPO-OBSERVATION-MARKER\n", encoding="utf-8",
    )
    wrapped = runner._wrap_goal(
        "implement_feature", "GOAL-TOKEN", workspace_dir=str(tmp_path),
    )
    assert "Quality bar" in wrapped  # universal reaches the prompt
    assert "REPO-OBSERVATION-MARKER" in wrapped  # so does per-repo
    assert wrapped.rstrip().endswith("GOAL-TOKEN")  # goal still lands last
    assert wrapped != "GOAL-TOKEN"
    assert "AGENTS.md" in wrapped  # from embedded _CONTEXT_PREAMBLE


# ---- _run_hook behavior (universal + per-repo) ------------------------------


def test_run_hook_returns_empty_when_nothing_exists(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_HOOKS_DIR", str(tmp_path / "nonexistent"))
    warnings = runner._run_hook("pre-run", str(tmp_path), "implement_feature", "task-id")
    assert warnings == []


def test_pre_run_hook_executes(runner, hook_dir, tmp_path):
    # workspace must exist; pre-run snapshots HEAD if git, otherwise quiet.
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = runner._run_hook("pre-run", str(ws), "implement_feature", "task-id")
    # non-git workspace → no snapshot → no warnings on happy path
    assert all("fatal" not in w.lower() for w in warnings)


def test_post_run_hook_warns_on_e2e_without_playwright_in_verify(runner, hook_dir, tmp_path):
    # Simulate: pre-run snapshotted a HEAD, agent added an e2e spec, verify_cmd
    # is pytest-only. Post-run should warn with the [post-run] tag.
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

    warnings = runner._run_hook(
        "post-run", str(ws), "implement_feature", "task-id", "pytest -q"
    )
    assert any("[post-run]" in w for w in warnings)
    joined = "\n".join(warnings)
    assert "browser tests added but verify_cmd" in joined
    assert "smoke.spec.ts" in joined


def test_per_repo_hook_runs_alongside_universal(runner, hook_dir, tmp_path):
    # When the workspace ships its own .agent/hooks/<name>.sh, runner fires the
    # universal hook AND the per-repo hook; both contribute to the warning list
    # with distinct tags so the goal layer can tell them apart.
    ws = tmp_path / "ws"
    (ws / ".agent" / "hooks").mkdir(parents=True)
    repo_hook = ws / ".agent" / "hooks" / "pre-run.sh"
    repo_hook.write_text("#!/usr/bin/env bash\necho 'repo-pre-run-fired'\n")
    repo_hook.chmod(0o755)

    warnings = runner._run_hook("pre-run", str(ws), "implement_feature", "task-id")
    repo_warnings = [w for w in warnings if w.startswith("[pre-run:repo]")]
    assert repo_warnings, f"expected per-repo warning, got: {warnings}"
    assert "repo-pre-run-fired" in repo_warnings[0]


def test_per_repo_hook_missing_does_not_crash(runner, hook_dir, tmp_path):
    # Workspaces WITHOUT .agent/hooks/ still work — per-repo layer is purely
    # opt-in, no warnings emitted on its behalf.
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = runner._run_hook("pre-run", str(ws), "implement_feature", "task-id")
    repo_warnings = [w for w in warnings if "[pre-run:repo]" in w]
    assert repo_warnings == []


def test_common_skill_mentions_per_repo_skills(runner, skill_dir):
    # The _common skill is what tells the agent to ls .agent/skills/ — that's
    # the entire discovery mechanism for the per-repo layer.
    bundle = runner._load_skills("implement_feature")
    assert ".agent/skills/" in bundle
    assert "PROJECT-OWNED" in bundle
