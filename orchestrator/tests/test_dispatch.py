"""Unit tests for the deterministic dispatch/reap/watchdog passes.

Pure-function tests. No Claude, no LangGraph, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.dispatch import (
    WATCHDOG_GRACE_SECONDS,
    compute_watchdog_deadline,
    find_completion_artifact,
    is_ghosted,
    load_spec,
    mark_dispatched,
    mark_ghosted,
    persist_spec,
    reap_spec,
)
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


def make_spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-18-test-spec-aaaa",
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="add hello.md to dsdevq/lifekit-stack",
        kind=TaskKind.code,
        target_repo="dsdevq/lifekit-stack",
        acceptance_criteria=["hello.md exists on the PR branch"],
        budget=Budget(max_runtime_seconds=1800),
        status=TaskStatus.ready,
    )
    base.update(overrides)
    return TaskSpec(**base)


def test_compute_watchdog_deadline_adds_budget_plus_grace():
    t = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    deadline = compute_watchdog_deadline(t, budget_seconds=1800)
    assert deadline == t + timedelta(seconds=1800 + WATCHDOG_GRACE_SECONDS)


def test_mark_dispatched_writes_all_fields():
    spec = make_spec()
    dispatched = mark_dispatched(
        spec,
        dispatch_run_id="run-xyz",
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert dispatched.status == TaskStatus.dispatched_subagent
    assert dispatched.dispatch_target == "subagent"
    assert dispatched.dispatch_run_id == "run-xyz"
    assert dispatched.dispatched_at == datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    assert dispatched.watchdog_deadline is not None
    assert dispatched.watchdog_deadline == datetime(2026, 5, 18, 12, 35, tzinfo=timezone.utc)


def test_is_ghosted_false_before_deadline():
    deadline = datetime(2026, 5, 18, 13, 0, tzinfo=timezone.utc)
    spec = make_spec(
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        watchdog_deadline=deadline,
    )
    earlier = datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    assert is_ghosted(spec, current_time=earlier) is False


def test_is_ghosted_true_past_deadline():
    deadline = datetime(2026, 5, 18, 13, 0, tzinfo=timezone.utc)
    spec = make_spec(
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        watchdog_deadline=deadline,
    )
    later = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
    assert is_ghosted(spec, current_time=later) is True


def test_is_ghosted_false_when_already_completed():
    deadline = datetime(2026, 5, 18, 13, 0, tzinfo=timezone.utc)
    spec = make_spec(
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        watchdog_deadline=deadline,
        completed_at=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc),
    )
    later = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
    assert is_ghosted(spec, current_time=later) is False


def test_is_ghosted_false_when_no_watchdog_deadline_field():
    """Older specs from before this mechanism existed must NOT be watchdog'd."""
    spec = make_spec(
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        watchdog_deadline=None,
    )
    later = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
    assert is_ghosted(spec, current_time=later) is False


def test_mark_ghosted_writes_blocked_status_and_summary():
    spec = make_spec(status=TaskStatus.dispatched_subagent)
    ghosted = mark_ghosted(spec)
    assert ghosted.status == TaskStatus.blocked
    assert ghosted.completed_at is not None
    assert "runner_silent_past_deadline" in (ghosted.result_summary or "")


def test_find_completion_artifact_result_json(tmp_path: Path):
    (tmp_path / "result.json").write_text('{"task_id": "x", "status": "done"}')
    artifact = find_completion_artifact(tmp_path, kind="code")
    assert artifact is not None
    assert artifact.name == "result.json"


def test_find_completion_artifact_findings_md(tmp_path: Path):
    (tmp_path / "findings.md").write_text("# Findings\n\nthings")
    artifact = find_completion_artifact(tmp_path, kind="research")
    assert artifact is not None
    assert artifact.name == "findings.md"


def test_find_completion_artifact_subagent_complete_event(tmp_path: Path):
    log = tmp_path / "run.log.jsonl"
    log.write_text(
        '{"ts": "2026-05-18T10:00:00Z", "event": "started"}\n'
        '{"ts": "2026-05-18T10:30:00Z", "event": "subagent_complete"}\n'
    )
    artifact = find_completion_artifact(tmp_path, kind="code")
    assert artifact is not None
    assert artifact.name == "run.log.jsonl"


def test_find_completion_artifact_none_when_nothing_there(tmp_path: Path):
    assert find_completion_artifact(tmp_path, kind="code") is None


def test_reap_spec_with_result_json(tmp_path: Path):
    artifact = tmp_path / "result.json"
    artifact.write_text(
        json.dumps(
            {
                "task_id": "x",
                "status": "done",
                "completed_at": "2026-05-18T10:30:00+00:00",
                "pr_url": "https://github.com/dsdevq/x/pull/1",
                "branch": "kit/x",
                "notes": "shipped",
            }
        )
    )
    spec = make_spec(status=TaskStatus.dispatched_subagent)
    reaped = reap_spec(spec, artifact)
    assert reaped.status == TaskStatus.done
    assert reaped.result_summary == "shipped"


def test_reap_spec_with_findings_md_uses_first_line(tmp_path: Path):
    artifact = tmp_path / "findings.md"
    artifact.write_text("# Codex BuildEngine migration findings\n\nbody...")
    spec = make_spec(kind=TaskKind.research, status=TaskStatus.dispatched_subagent)
    reaped = reap_spec(spec, artifact)
    assert reaped.status == TaskStatus.done
    assert "Codex BuildEngine migration findings" in (reaped.result_summary or "")


def test_persist_and_load_roundtrip(tmp_path: Path):
    spec = make_spec()
    spec_path = tmp_path / "spec.yaml"
    persist_spec(spec, spec_path)
    loaded = load_spec(spec_path)
    assert loaded.task_id == spec.task_id
    assert loaded.kind == spec.kind
    assert loaded.budget.max_runtime_seconds == spec.budget.max_runtime_seconds


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
