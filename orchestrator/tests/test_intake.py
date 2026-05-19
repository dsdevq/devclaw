"""Tests for task_intake.

Mocks `run_claude` — no real Claude calls. Validates spec generation, project routing, validation failures, and file-on-disk placement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.dispatch import load_spec
from orchestrator.intake import intake
from orchestrator.runners._subprocess import SubprocessResult
from orchestrator.state.models import RequesterRoute, TaskKind, TaskStatus


def _mock_claude_json(payload: dict, *, status: str = "done"):
    """Return a SubprocessResult that looks like claude --print succeeded with this JSON."""
    return SubprocessResult(
        status=status,
        parsed_json=payload,
        raw_stdout=str(payload),
        raw_stderr="",
        returncode=0,
    )


def _mock_claude_failure(blocker: str = "no_parseable_result_json"):
    return SubprocessResult(
        status="blocked",
        parsed_json=None,
        raw_stdout="",
        raw_stderr="",
        returncode=0,
        blocker=blocker,
    )


def _write_project_settings(life: Path, slug: str, github_repo: str) -> Path:
    """Create a stub project at life/projects/<slug>/settings.yaml with the given github_repo."""
    project_dir = life / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    settings = project_dir / "settings.yaml"
    settings.write_text(f"github_repo: {github_repo}\n")
    return settings


def test_intake_creates_code_spec_for_repo_intent(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    _write_project_settings(life, "lifekit-stack", "dsdevq/lifekit-stack")

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "code",
                "target_repo": "dsdevq/lifekit-stack",
                "target_branch": "main",
                "project": "lifekit-stack",
                "acceptance_criteria": ["the typo is fixed in README.md"],
                "budget_seconds": 900,
                "notes": "small typo fix in lifekit-stack README",
            }
        ),
    ):
        spec = intake(
            "fix the typo 'depployment' in lifekit-stack README",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    assert spec.kind == TaskKind.code
    assert spec.target_repo == "dsdevq/lifekit-stack"
    assert spec.project == "lifekit-stack"
    assert spec.status == TaskStatus.ready
    assert spec.budget.max_runtime_seconds == 900
    # spec written to project-bound location
    expected = life / "projects" / "lifekit-stack" / "tasks" / spec.task_id / "spec.yaml"
    assert expected.is_file()
    reloaded = load_spec(expected)
    assert reloaded.kind == TaskKind.code


def test_intake_creates_atomic_research_spec_when_no_project(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "research",
                "target_repo": None,
                "project": None,
                "acceptance_criteria": ["findings.md cites at least 3 sources"],
                "budget_seconds": 1800,
                "notes": "research question",
            }
        ),
    ):
        spec = intake(
            "research the current state of distributed durable execution engines",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    assert spec.kind == TaskKind.research
    assert spec.project is None
    assert spec.target_repo is None
    # spec written to atomic location, not project-bound
    expected = life / "tasks" / spec.task_id / "spec.yaml"
    assert expected.is_file()


def test_intake_defaults_budget_when_omitted(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "research",
                # no budget_seconds key
            }
        ),
    ):
        spec = intake("x", requester_route=RequesterRoute(channel="test", to="t"), life_root=life)

    assert spec is not None
    assert spec.budget.max_runtime_seconds == 1800  # default


def test_intake_returns_none_when_claude_no_json(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_failure("no_parseable_result_json"),
    ):
        spec = intake("x", requester_route=RequesterRoute(channel="test", to="t"), life_root=life)

    assert spec is None
    # no spec.yaml was written
    assert list(life.glob("**/spec.yaml")) == []


def test_intake_returns_none_when_claude_emits_bad_kind(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json({"kind": "invalid-kind"}),
    ):
        spec = intake("x", requester_route=RequesterRoute(channel="test", to="t"), life_root=life)

    assert spec is None


def test_intake_respects_explicit_task_id(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json({"kind": "research"}),
    ):
        spec = intake(
            "x",
            requester_route=RequesterRoute(channel="test", to="t"),
            life_root=life,
            task_id="custom-task-id-99",
        )

    assert spec is not None
    assert spec.task_id == "custom-task-id-99"
    assert (life / "tasks" / "custom-task-id-99" / "spec.yaml").is_file()


def test_intake_writes_verbatim_intent(tmp_path: Path):
    """Spec.verbatim_intent is the operator's literal text, not Claude's interpretation."""
    life = tmp_path / "life"
    life.mkdir()
    intent = "fix the typo 'depployment' in lifekit-stack README"

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json({"kind": "code", "target_repo": "dsdevq/x"}),
    ):
        spec = intake(intent, requester_route=RequesterRoute(channel="test", to="t"), life_root=life)

    assert spec is not None
    assert spec.verbatim_intent == intent


# ─── Project-routing by target_repo lookup in projects/*/settings.yaml ───────


def test_intake_routes_to_project_bucket_when_target_repo_matches(tmp_path: Path):
    """target_repo matches a project's settings.yaml → spec lands in projects/<slug>/tasks/."""
    life = tmp_path / "life"
    life.mkdir()
    _write_project_settings(life, "devclaw", "dsdevq/devclaw")
    # an unrelated project to make sure we don't accidentally match it
    _write_project_settings(life, "finance-sentry", "dsdevq/finance-sentry")

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "code",
                "target_repo": "dsdevq/devclaw",
                "target_branch": "main",
                "project": None,  # Claude did NOT set project — we derive it from the filesystem
                "acceptance_criteria": ["foo"],
                "budget_seconds": 1800,
            }
        ),
    ):
        spec = intake(
            "tweak something in devclaw",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    project_path = life / "projects" / "devclaw" / "tasks" / spec.task_id / "spec.yaml"
    flat_path = life / "tasks" / spec.task_id / "spec.yaml"
    assert project_path.is_file()
    assert not flat_path.exists()


def test_intake_routes_to_flat_bucket_when_target_repo_has_no_project_match(tmp_path: Path):
    """target_repo is set but no project's settings.yaml claims it → falls back to flat bucket."""
    life = tmp_path / "life"
    life.mkdir()
    _write_project_settings(life, "lifekit-stack", "dsdevq/lifekit-stack")

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "code",
                "target_repo": "someone-else/unknown-repo",
                "target_branch": "main",
                "project": None,
                "acceptance_criteria": ["something"],
            }
        ),
    ):
        spec = intake(
            "patch some external repo we don't track",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    flat_path = life / "tasks" / spec.task_id / "spec.yaml"
    assert flat_path.is_file()
    # no project bucket got created for this spec
    assert list(life.glob("projects/*/tasks/*/spec.yaml")) == []


def test_intake_routes_to_flat_bucket_when_target_repo_missing(tmp_path: Path):
    """No target_repo at all (e.g. chore/research) → spec always lands in flat bucket."""
    life = tmp_path / "life"
    life.mkdir()
    # a known project exists — we should NOT route to it without a target_repo signal
    _write_project_settings(life, "devclaw", "dsdevq/devclaw")

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "research",
                "target_repo": None,
                "project": None,
                "acceptance_criteria": ["findings.md exists"],
            }
        ),
    ):
        spec = intake(
            "research distributed durable execution engines",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    flat_path = life / "tasks" / spec.task_id / "spec.yaml"
    assert flat_path.is_file()
    assert list(life.glob("projects/*/tasks/*/spec.yaml")) == []


def test_intake_picks_most_recent_settings_when_target_repo_matches_multiple(tmp_path: Path, caplog):
    """If two projects' settings.yaml both claim the same github_repo, pick the newer one and WARN."""
    import logging
    import os
    import time

    life = tmp_path / "life"
    life.mkdir()

    older = _write_project_settings(life, "older-project", "dsdevq/shared-repo")
    # ensure distinct mtimes regardless of filesystem granularity
    old_time = time.time() - 3600
    os.utime(older, (old_time, old_time))
    newer = _write_project_settings(life, "newer-project", "dsdevq/shared-repo")
    new_time = time.time()
    os.utime(newer, (new_time, new_time))

    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "code",
                "target_repo": "dsdevq/shared-repo",
                "project": None,
                "acceptance_criteria": ["x"],
            }
        ),
    ), caplog.at_level(logging.WARNING, logger="orchestrator.intake"):
        spec = intake(
            "do stuff in shared-repo",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
        )

    assert spec is not None
    expected = life / "projects" / "newer-project" / "tasks" / spec.task_id / "spec.yaml"
    assert expected.is_file()
    assert not (life / "projects" / "older-project" / "tasks" / spec.task_id / "spec.yaml").exists()
    # a WARN was emitted naming the conflict
    assert any(
        "matched multiple projects" in rec.message and "newer-project" in rec.message
        for rec in caplog.records
    )
