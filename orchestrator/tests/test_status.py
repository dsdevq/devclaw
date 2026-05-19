"""Tests for the status lookup used by both `devclaw-orchestrator status`
and the MCP `devclaw_status` tool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.dispatch import persist_spec
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from orchestrator.status import lookup_task_status


def _make_spec(task_id: str, status: TaskStatus = TaskStatus.ready) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        created_at=datetime(2026, 5, 19, tzinfo=UTC),
        created_by="test",
        requester_route=RequesterRoute(channel="cli", to="t"),
        verbatim_intent="x",
        kind=TaskKind.code,
        target_repo="dsdevq/devclaw",
        acceptance_criteria=["y"],
        budget=Budget(max_runtime_seconds=600),
        status=status,
    )


def test_status_unknown_when_no_spec(tmp_path: Path):
    info = lookup_task_status("does-not-exist", life_root=tmp_path)
    assert info["state"] == "unknown"
    assert info["pr_url"] is None
    assert info["spec_path"] is None


def test_status_ready_from_atomic_dir(tmp_path: Path):
    task_id = "2026-05-19-foo-aaaa"
    task_dir = tmp_path / "tasks" / task_id
    task_dir.mkdir(parents=True)
    persist_spec(_make_spec(task_id, TaskStatus.ready), task_dir / "spec.yaml")

    info = lookup_task_status(task_id, life_root=tmp_path)
    assert info["state"] == "ready"
    assert info["last_action"] == "ready"
    assert info["spec_path"].endswith(f"{task_id}/spec.yaml")


def test_status_project_bound_dir(tmp_path: Path):
    task_id = "2026-05-19-foo-bbbb"
    task_dir = tmp_path / "projects" / "devclaw" / "tasks" / task_id
    task_dir.mkdir(parents=True)
    persist_spec(_make_spec(task_id, TaskStatus.dispatched_subagent), task_dir / "spec.yaml")

    info = lookup_task_status(task_id, life_root=tmp_path)
    assert info["state"] == "dispatched-subagent"


def test_status_uses_result_json_when_present(tmp_path: Path):
    task_id = "2026-05-19-foo-cccc"
    task_dir = tmp_path / "tasks" / task_id
    task_dir.mkdir(parents=True)
    persist_spec(_make_spec(task_id, TaskStatus.dispatched_subagent), task_dir / "spec.yaml")
    (task_dir / "result.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": "done",
                "completed_at": "2026-05-19T10:00:00Z",
                "pr_url": "https://github.com/dsdevq/devclaw/pull/99",
            }
        )
    )

    info = lookup_task_status(task_id, life_root=tmp_path)
    assert info["state"] == "done"
    assert info["last_action"] == "done"
    assert info["pr_url"] == "https://github.com/dsdevq/devclaw/pull/99"
    assert info["completed_at"] == "2026-05-19T10:00:00Z"
