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


def test_intake_creates_code_spec_for_repo_intent(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

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
