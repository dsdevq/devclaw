"""Pure data + row mappers for the state store.

No shared state, no connection — just the ``Task``/``Program``/``TaskEvent``
dataclasses, their wire-shape ``to_dict`` (camelCase, to match the original
TypeScript output), the ``sqlite3.Row`` → dataclass mappers, the status/kind
literals, and the shared busy-timeout constant.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal, Optional

# cancelled — deliberately aborted by a client (distinct from 'failed', which is
#   an execution error). Terminal, so crash recovery (which only revives
#   'running' rows) never resurrects it — an abort stays aborted across restarts.
TaskStatus = Literal["pending", "running", "done", "failed", "cancelled"]
TaskKind = Literal["implement_feature", "fix_bug", "review_repository", "onboard"]
# Programs hold a DAG of tasks decomposed from a single high-level goal.
#   planning  — planner still decomposing (claude subprocess in flight)
#   running   — tasks exist, none failed/cancelled, not all terminal yet
#   done      — every task is 'done'
#   failed    — planner failed OR any task failed (sticky; siblings are not
#               scheduled after a failure — see TaskQueue for the policy)
#   cancelled — aborted by a client; a cancelled child is sticky like a failure
ProgramStatus = Literal["planning", "running", "done", "failed", "cancelled"]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Task:
    id: str
    kind: TaskKind
    status: TaskStatus
    workspace_dir: str
    goal: str
    notify_url: Optional[str]
    result_json: Optional[str]
    error: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    program_id: Optional[str]
    depends_on: list[str]
    order_idx: Optional[int]
    #: the spec milestone this task serves (set by plan-from-spec; else None)
    milestone: Optional[str]
    #: optional verify-gate command run after the agent finishes; its exit code
    #: decides done-vs-failed (the agent's self-report is not trusted). None → no gate.
    verify_cmd: Optional[str]
    #: deliver the change as a branch/PR after a successful run (open_pr tasks)
    deliver: bool
    #: the delivered PR URL (or None if not delivered / only a local branch)
    pr_url: Optional[str]
    #: Planner-chosen PR title (see Action.title). Optional; when None, delivery
    #: falls back to the engineer's own commit subject or the goal-derived
    #: heuristic.
    title: Optional[str] = None
    #: The durable goal that owns this task. Set when the goal heartbeat
    #: dispatches a task; None for standalone user-initiated dispatches
    #: (``dispatch_task``). Orthogonal to ``program_id`` (ephemeral DAG-run
    #: pointer) — a task can carry both, one, or neither.
    parent_goal_id: Optional[str] = None
    #: How many times this task was requeued by a usage-limit pause. Bounds the
    #: pause→requeue→re-run loop: a permanently-failing task whose error text
    #: happens to match the quota/rate regexes would otherwise loop forever
    #: (the workspace breaker only counts *failed* rows, and a paused task
    #: never becomes one).
    pause_count: int = 0
    #: True when this task is *generated scaffolding* (L3, #222) — set from the
    #: decomposer-tagged ChecklistItem.scaffold via the goal dispatch path. It
    #: makes the queue skip ONLY the adversarial review gate (a huge generated
    #: diff crashes it and shouldn't be diff-reviewed anyway). The verify/build
    #: gate + test-integrity scan STILL run — a scaffold task that doesn't build
    #: or that guts tests still fails. Defaulted so existing rows/tests are
    #: unaffected.
    scaffold: bool = False
    #: The PlannedTask key this program-child row was persisted from (ADR 0003
    #: stage 2). For a one-shot goal's program the key IS the checklist item
    #: id — the settle path's child→item join. None for standalone tasks and
    #: rows that predate the column.
    plan_key: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "workspaceDir": self.workspace_dir,
            "goal": self.goal,
            "notifyUrl": self.notify_url,
            "resultJson": self.result_json,
            "error": self.error,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "programId": self.program_id,
            "dependsOn": self.depends_on,
            "orderIdx": self.order_idx,
            "milestone": self.milestone,
            "verifyCmd": self.verify_cmd,
            "deliver": self.deliver,
            "prUrl": self.pr_url,
            "title": self.title,
            "parentGoalId": self.parent_goal_id,
            "pauseCount": self.pause_count,
            "scaffold": self.scaffold,
        }


@dataclass
class Program:
    id: str
    goal: str
    workspace_dir: str
    notify_url: Optional[str]
    status: ProgramStatus
    error: Optional[str]
    created_at: int
    completed_at: Optional[int]
    #: When True, every task the decomposer creates for this program inherits
    #: ``deliver=True`` — the standing-goal / reviewable-slice contract. When
    #: False (legacy default), program tasks commit directly and never open a
    #: PR (the pre-2026-07-03 behavior).
    open_pr: bool = False
    #: Gate command the decomposer's tasks inherit. None → no gate (matches
    #: legacy behavior); when set, child tasks run this after the agent
    #: finishes and only succeed on exit 0.
    verify_cmd: Optional[str] = None
    #: Durable goal-owner pointer (2026-07-10), mirroring tasks.parent_goal_id.
    #: Without it a goal whose STATUS.md in_flight ref is lost (crash mid-write)
    #: has NO way to rediscover its own running/failed program — the 2026-07-09
    #: closeloop-mission-v2 dead night. Null for standalone start_program calls.
    parent_goal_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "workspaceDir": self.workspace_dir,
            "notifyUrl": self.notify_url,
            "status": self.status,
            "error": self.error,
            "createdAt": self.created_at,
            "completedAt": self.completed_at,
            "openPr": self.open_pr,
            "verifyCmd": self.verify_cmd,
            "parentGoalId": self.parent_goal_id,
        }


@dataclass
class TaskEvent:
    id: int
    task_id: str
    program_id: Optional[str]
    type: str
    source: str
    payload_json: str
    ts: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "taskId": self.task_id,
            "programId": self.program_id,
            "type": self.type,
            "source": self.source,
            "payloadJson": self.payload_json,
            "ts": self.ts,
        }


def _row_to_task(r: sqlite3.Row) -> Task:
    depends_on: list[str] = []
    if r["depends_on"]:
        try:
            parsed = json.loads(r["depends_on"])
            if isinstance(parsed, list):
                depends_on = [x for x in parsed if isinstance(x, str)]
        except json.JSONDecodeError:
            # tolerate a corrupt depends_on cell — treat as no deps
            pass
    return Task(
        id=r["id"],
        kind=r["kind"],
        status=r["status"],
        workspace_dir=r["workspace_dir"],
        goal=r["goal"],
        notify_url=r["notify_url"],
        result_json=r["result_json"],
        error=r["error"],
        created_at=r["created_at"],
        started_at=r["started_at"],
        completed_at=r["completed_at"],
        program_id=r["program_id"],
        depends_on=depends_on,
        order_idx=r["order_idx"],
        milestone=r["milestone"],
        verify_cmd=r["verify_cmd"],
        deliver=bool(r["deliver"]),
        pr_url=r["pr_url"],
        title=r["title"] if "title" in r.keys() else None,
        parent_goal_id=(
            r["parent_goal_id"] if "parent_goal_id" in r.keys() else None
        ),
        pause_count=(
            r["pause_count"] if "pause_count" in r.keys() and r["pause_count"] is not None else 0
        ),
        scaffold=(
            bool(r["scaffold"]) if "scaffold" in r.keys() and r["scaffold"] is not None else False
        ),
        plan_key=r["plan_key"] if "plan_key" in r.keys() else None,
    )


def _row_to_program(r: sqlite3.Row) -> Program:
    return Program(
        id=r["id"],
        goal=r["goal"],
        workspace_dir=r["workspace_dir"],
        notify_url=r["notify_url"],
        status=r["status"],
        error=r["error"],
        created_at=r["created_at"],
        completed_at=r["completed_at"],
        open_pr=bool(r["open_pr"]) if "open_pr" in r.keys() else False,
        verify_cmd=r["verify_cmd"] if "verify_cmd" in r.keys() else None,
        parent_goal_id=(
            r["parent_goal_id"] if "parent_goal_id" in r.keys() else None
        ),
    )


def _row_to_event(r: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=r["id"],
        task_id=r["task_id"],
        program_id=r["program_id"],
        type=r["type"],
        source=r["source"],
        payload_json=r["payload_json"],
        ts=r["ts"],
    )


#: How long a blocked writer waits for the lock before raising
#: ``sqlite3.OperationalError: database is locked``. WAL gives concurrent reads +
#: a single writer, but the default busy_timeout is 0 — so a *separate* process
#: (e.g. the ``devclaw`` CLI) writing while the server holds the write lock fails
#: instantly instead of waiting its turn. A few seconds lets contending writers
#: queue politely. Shared default with ``project_registry`` (same db file).
SQLITE_BUSY_TIMEOUT_MS = 5000
