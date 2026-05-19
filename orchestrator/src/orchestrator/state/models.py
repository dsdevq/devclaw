"""Pydantic models mirroring devclaw's spec.yaml / dag.yaml / result.json shapes.

Kept structurally close to the markdown-skill schemas so the migration is translation, not redesign. Once the port is complete, these become the single source of truth and the yaml files become an export format.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TaskKind(str, Enum):
    code = "code"
    research = "research"
    draft = "draft"
    chore = "chore"
    decision = "decision"


class TaskStatus(str, Enum):
    ready = "ready"
    dispatched_subagent = "dispatched-subagent"
    dispatched_build = "dispatched-build"
    dispatched_human = "dispatched-human"
    done = "done"
    blocked = "blocked"


class RunStatus(str, Enum):
    in_progress = "in_progress"
    completed = "completed"
    blocked = "blocked"
    aborted = "aborted"


class RunnerStatus(str, Enum):
    pending = "pending"
    dispatched = "dispatched"
    claimed_done = "claimed_done"
    verified_done = "verified_done"
    verification_failed = "verification_failed"


class VerifierStatus(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"


class RequesterRoute(BaseModel):
    channel: Literal["telegram", "cli", "test"] = "telegram"
    to: str
    account_id: str = "default"


class Budget(BaseModel):
    max_runtime_seconds: int = Field(default=1800, ge=60, le=14400)


class Evidence(BaseModel):
    tests_passed: bool | None = None
    pr_url: str | None = None
    files_changed: list[str] | None = None
    result_summary: str | None = None
    verification_failure_reason: str | None = None
    reaped_by_dispatcher: bool | None = None
    ghosted_by_watchdog: bool | None = None


class TaskSpec(BaseModel):
    """One unit of dispatchable work. Mirrors `spec.yaml` in the markdown world."""

    task_id: str
    created_at: datetime
    created_by: str
    requester_route: RequesterRoute
    verbatim_intent: str

    kind: TaskKind
    acceptance_criteria: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)

    target_repo: str | None = None
    target_branch: str = "main"

    project: str | None = None
    run: str | None = None
    run_node: str | None = None
    proposal_path: Path | None = None
    context_files: list[Path] = Field(default_factory=list)

    status: TaskStatus = TaskStatus.ready
    dispatch_target: str | None = None
    dispatch_run_id: str | None = None
    dispatched_at: datetime | None = None
    watchdog_deadline: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None

    contract_class: Literal["atomic", "contract", "architecture"] | None = None
    merged_at: datetime | None = None

    depends_on: list[str] = Field(default_factory=list)


class DagNode(BaseModel):
    id: str
    title: str
    kind: TaskKind
    depends_on: list[str] = Field(default_factory=list)
    budget_seconds: int = 1800
    target_repo: str | None = None
    target_branch: str = "main"
    acceptance_criteria: list[str] = Field(default_factory=list)

    runner_status: RunnerStatus = RunnerStatus.pending
    verifier_status: VerifierStatus = VerifierStatus.pending
    spec_path: Path | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    verified_at: datetime | None = None
    retried: bool = False
    evidence: Evidence = Field(default_factory=Evidence)


class Run(BaseModel):
    """Multi-task DAG run. Mirrors `dag.yaml` in the markdown world."""

    run_id: str
    project: str
    proposal: Path | None = None
    created_at: datetime
    status: RunStatus = RunStatus.in_progress
    tasks: list[DagNode]


class Result(BaseModel):
    """Runner output — mirrors `result.json`. Written once per task at completion."""

    task_id: str
    status: Literal["done", "blocked"]
    completed_at: datetime
    pr_url: str | None = None
    branch: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    tests_passed: bool | None = None
    notes: str | None = None
    runtime_seconds: int | None = None
    blocker: str | None = None
    to_resume: str | None = None


class GraphState(BaseModel):
    """The LangGraph state for a single task pipeline.

    For the v0.0.1 slice we model a single TaskSpec going through dispatch → runner → verify → complete. The Run/DAG-level orchestration (the project_curator equivalent) is a separate supervisor graph composed on top once this slice is solid.
    """

    spec: TaskSpec
    result: Result | None = None
    error: str | None = None  # set by terminal failure paths
    retry_count: int = 0  # incremented by the retry-once path on verification_failed
