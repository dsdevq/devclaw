"""SQLite state store for DevClaw tasks.

Tracks every task DevClaw has been asked to run, its current status, and the
result (or error) once it terminates. ``sqlite3`` is sync; a re-entrant lock
serializes access because FastMCP may touch the store from the event loop and
from background tasks. WAL mode gives concurrent reads with a single writer.

Wire shapes (``to_dict``) are camelCase to match the original TypeScript
output, so MCP consumers keep working across the rewrite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("DEVCLAW_SQLITE_BUSY_TIMEOUT_MS", "5000"))


class StateStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")  # concurrent reads, single writer
        self._db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")  # wait, don't fail-fast
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._bootstrap()

    def _bootstrap(self) -> None:
        with self._lock:
            # (1) Create tables (idempotent). CREATE TABLE for `tasks` is the
            # current schema; pre-existing DBs get caught up by the ALTERs below.
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  id              TEXT PRIMARY KEY,
                  kind            TEXT NOT NULL DEFAULT 'implement_feature',
                  status          TEXT NOT NULL,
                  workspace_dir   TEXT NOT NULL,
                  goal            TEXT NOT NULL,
                  notify_url      TEXT,
                  result_json     TEXT,
                  error           TEXT,
                  created_at      INTEGER NOT NULL,
                  started_at      INTEGER,
                  completed_at    INTEGER,
                  program_id      TEXT,
                  depends_on      TEXT,
                  order_idx       INTEGER,
                  milestone       TEXT,
                  verify_cmd      TEXT,
                  deliver         INTEGER NOT NULL DEFAULT 0,
                  pr_url          TEXT,
                  title           TEXT,
                  parent_goal_id  TEXT
                );

                CREATE TABLE IF NOT EXISTS programs (
                  id              TEXT PRIMARY KEY,
                  goal            TEXT NOT NULL,
                  workspace_dir   TEXT NOT NULL,
                  notify_url      TEXT,
                  status          TEXT NOT NULL,
                  error           TEXT,
                  created_at      INTEGER NOT NULL,
                  completed_at    INTEGER,
                  open_pr         INTEGER NOT NULL DEFAULT 0,
                  verify_cmd      TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                  id              INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id         TEXT NOT NULL,
                  program_id      TEXT,
                  type            TEXT NOT NULL,
                  source          TEXT NOT NULL DEFAULT '',
                  payload_json    TEXT NOT NULL,
                  ts              INTEGER NOT NULL
                );

                -- small key/value for process-wide flags (e.g. the global quota
                -- pause). Survives restart so a pause isn't lost on a recreate.
                CREATE TABLE IF NOT EXISTS meta (
                  key             TEXT PRIMARY KEY,
                  value           TEXT NOT NULL
                );

                -- Per-goal-tick trace events: every cognition call, dispatch,
                -- delivery, subprocess, notify, etc. that a heartbeat tick
                -- emitted. Grouped by trace_id (one per tick) so the full
                -- causal chain of a tick can be replayed. Append-only; never
                -- mutated. Read by the get_trace MCP tool and the dashboard.
                CREATE TABLE IF NOT EXISTS traces (
                  id              INTEGER PRIMARY KEY AUTOINCREMENT,
                  trace_id        TEXT NOT NULL,
                  goal_id         TEXT NOT NULL,
                  kind            TEXT NOT NULL,
                  ts              INTEGER NOT NULL,
                  payload_json    TEXT NOT NULL
                );
                """
            )

            # (2) Forward-compat ALTERs for DBs created by older versions. Each
            # is idempotent — swallow duplicate-column errors. MUST run before
            # the indexes below, which reference these columns.
            for sql in (
                "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'implement_feature'",
                "ALTER TABLE tasks ADD COLUMN notify_url TEXT",
                "ALTER TABLE tasks ADD COLUMN program_id TEXT",
                "ALTER TABLE tasks ADD COLUMN depends_on TEXT",
                "ALTER TABLE tasks ADD COLUMN order_idx INTEGER",
                "ALTER TABLE tasks ADD COLUMN milestone TEXT",
                "ALTER TABLE tasks ADD COLUMN verify_cmd TEXT",
                "ALTER TABLE tasks ADD COLUMN deliver INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN pr_url TEXT",
                "ALTER TABLE tasks ADD COLUMN title TEXT",
                # Program-level PR discipline (2026-07-03) — inherited by
                # child tasks so start_program under a standing goal ships
                # reviewable-slice PRs, not direct-to-main commits.
                "ALTER TABLE programs ADD COLUMN open_pr INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE programs ADD COLUMN verify_cmd TEXT",
                # Durable goal-owner pointer (2026-07-04) — set by the goal
                # heartbeat when it dispatches a task; null for standalone
                # dispatch_task calls. Orthogonal to program_id.
                "ALTER TABLE tasks ADD COLUMN parent_goal_id TEXT",
            ):
                try:
                    self._db.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # (3) Indexes — safe now that all referenced columns exist.
            self._db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_kind       ON tasks(kind);
                CREATE INDEX IF NOT EXISTS idx_tasks_program    ON tasks(program_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_parent_goal ON tasks(parent_goal_id);
                CREATE INDEX IF NOT EXISTS idx_programs_status  ON programs(status);
                CREATE INDEX IF NOT EXISTS idx_events_program   ON events(program_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_task      ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_goal      ON traces(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_trace     ON traces(trace_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_kind      ON traces(kind, id);
                """
            )
            self._db.commit()

    # ---- tasks ----------------------------------------------------------

    def create_task(
        self,
        *,
        id: str,
        kind: TaskKind,
        workspace_dir: str,
        goal: str,
        notify_url: Optional[str] = None,
        program_id: Optional[str] = None,
        depends_on: Optional[list[str]] = None,
        order_idx: Optional[int] = None,
        milestone: Optional[str] = None,
        verify_cmd: Optional[str] = None,
        deliver: bool = False,
        title: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks
                     (id, kind, status, workspace_dir, goal, notify_url, created_at,
                      program_id, depends_on, order_idx, milestone, verify_cmd, deliver,
                      title, parent_goal_id)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id,
                    kind,
                    workspace_dir,
                    goal,
                    notify_url,
                    _now_ms(),
                    program_id,
                    json.dumps(depends_on) if depends_on else None,
                    order_idx,
                    milestone,
                    verify_cmd,
                    1 if deliver else 0,
                    title,
                    parent_goal_id,
                ),
            )
            self._db.commit()

    def set_pr_url(self, task_id: str, pr_url: Optional[str]) -> None:
        """Record the delivered PR URL (or None for a local-only branch)."""
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET pr_url = ? WHERE id = ?", (pr_url, task_id)
            )
            self._db.commit()

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_now_ms(), task_id),
            )
            self._db.commit()

    def claim_pending(self, task_id: str) -> bool:
        """Atomically transition pending -> running. Returns True if THIS call
        won the race (caller must execute the task), False otherwise. Used by
        the DAG scheduler where multiple siblings finishing can both try to
        unblock the same downstream task."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_now_ms(), task_id),
            )
            self._db.commit()
            return cur.rowcount == 1

    def mark_done(self, task_id: str, result_json: str, pr_url: Optional[str] = None) -> None:
        """Settle a task 'done'. ``pr_url`` is written in the SAME statement as the
        status flip so 'done' is never observable before its delivery artifact —
        a poller (goalclaw) can't see done-without-PR and re-dispatch. COALESCE
        keeps an already-recorded pr_url when None is passed (program/plain path)."""
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'done', result_json = ?, "
                "pr_url = COALESCE(?, pr_url), completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (result_json, pr_url, _now_ms(), task_id),
            )
            self._db.commit()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (error, _now_ms(), task_id),
            )
            self._db.commit()

    def mark_task_cancelled(self, task_id: str) -> bool:
        """Abort a task. Transitions pending/running -> cancelled (terminal).
        Returns True iff a row moved — False if the task was already terminal
        (done/failed/cancelled), so a settle that lands a hair later can't
        clobber it (mark_done/mark_failed also guard on pending/running). Used
        by the queue's cancel path; the live execution is torn down separately."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'cancelled', completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (_now_ms(), task_id),
            )
            self._db.commit()
            return cur.rowcount == 1

    def cancel_program_pending_tasks(self, program_id: str) -> list[str]:
        """Bulk-cancel every PENDING task of a program (work not yet handed to
        the engine) so nothing new starts. Running tasks are torn down live by
        the queue, not here. Returns the cancelled task ids (for the audit log)."""
        with self._lock:
            ids = [
                r["id"]
                for r in self._db.execute(
                    "SELECT id FROM tasks WHERE program_id = ? AND status = 'pending'",
                    (program_id,),
                ).fetchall()
            ]
            if ids:
                self._db.execute(
                    "UPDATE tasks SET status = 'cancelled', completed_at = ? "
                    "WHERE program_id = ? AND status = 'pending'",
                    (_now_ms(), program_id),
                )
                self._db.commit()
        return ids

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        status: Optional[TaskStatus] = None,
        kind: Optional[TaskKind] = None,
        limit: int = 100,
    ) -> list[Task]:
        where: list[str] = []
        args: list[object] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks {where_sql} ORDER BY created_at DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    # ---- programs -------------------------------------------------------

    def create_program(
        self,
        *,
        id: str,
        goal: str,
        workspace_dir: str,
        notify_url: Optional[str] = None,
        open_pr: bool = False,
        verify_cmd: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO programs "
                "(id, goal, workspace_dir, notify_url, status, created_at, open_pr, verify_cmd) "
                "VALUES (?, ?, ?, ?, 'planning', ?, ?, ?)",
                (
                    id, goal, workspace_dir, notify_url, _now_ms(),
                    1 if open_pr else 0, verify_cmd,
                ),
            )
            self._db.commit()

    def mark_program_running(self, program_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'running' "
                "WHERE id = ? AND status = 'planning'",
                (program_id,),
            )
            self._db.commit()

    def mark_program_done(self, program_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'done', completed_at = ? "
                "WHERE id = ? AND status IN ('planning', 'running')",
                (_now_ms(), program_id),
            )
            self._db.commit()

    def mark_program_failed(self, program_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'failed', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('planning', 'running')",
                (error, _now_ms(), program_id),
            )
            self._db.commit()

    def mark_program_cancelled(self, program_id: str, error: Optional[str] = None) -> None:
        """Abort a program. Transitions planning/running -> cancelled (terminal).
        ``error`` carries a human reason (e.g. which task triggered it); it lands
        in the same column failures use, so notify payloads stay uniform."""
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'cancelled', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('planning', 'running')",
                (error, _now_ms(), program_id),
            )
            self._db.commit()

    def list_programs(self, *, limit: int = 100) -> list[Program]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM programs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_program(r) for r in rows]

    def get_program(self, program_id: str) -> Optional[Program]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM programs WHERE id = ?", (program_id,)
            ).fetchone()
        return _row_to_program(row) if row else None

    def list_program_tasks(self, program_id: str) -> list[Task]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE program_id = ? "
                "ORDER BY order_idx IS NULL, order_idx ASC, created_at ASC",
                (program_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    # ---- events ---------------------------------------------------------

    def append_event(
        self,
        *,
        task_id: str,
        program_id: Optional[str],
        type: str,
        source: str,
        payload_json: str,
        ts: Optional[int] = None,
    ) -> int:
        """Append one event row. Returns the auto-assigned monotonic id, which
        the SSE layer uses as the resume cursor (Last-Event-Id)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (task_id, program_id, type, source, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, program_id, type, source, payload_json, ts if ts is not None else _now_ms()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    # ---- traces (per-tick observability) --------------------------------

    def append_trace_event(
        self,
        *,
        trace_id: str,
        goal_id: str,
        kind: str,
        payload: dict,
        ts: Optional[int] = None,
    ) -> int:
        """Persist one trace event (cognition / tick / dispatch / delivery /
        subprocess / notify / note). Best-effort by convention — callers should
        not propagate exceptions out of telemetry. Returns the monotonic id."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO traces (trace_id, goal_id, kind, ts, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    trace_id,
                    goal_id,
                    kind,
                    ts if ts is not None else _now_ms(),
                    json.dumps(payload, default=str),
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def read_traces(
        self,
        *,
        goal_id: str,
        since_id: int = 0,
        limit: int = 500,
        kind: Optional[str] = None,
    ) -> list[dict]:
        """Read trace events for one goal in emission order. Pass ``since_id``
        to resume after a known cursor (exclusive); pass ``kind`` to filter
        (e.g. only cognition calls)."""
        sql = (
            "SELECT id, trace_id, goal_id, kind, ts, payload_json FROM traces "
            "WHERE goal_id = ? AND id > ?"
        )
        args: list[object] = [goal_id, since_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [
            {
                "id": r["id"],
                "trace_id": r["trace_id"],
                "goal_id": r["goal_id"],
                "kind": r["kind"],
                "ts": r["ts"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]

    def trace_totals(self, *, goal_id: str) -> dict:
        """Aggregate stats over all trace events for a goal: counts per kind,
        cognition total latency + estimated tokens. Cheap SQL — no LLM call."""
        with self._lock:
            counts = dict(
                self._db.execute(
                    "SELECT kind, COUNT(*) AS n FROM traces WHERE goal_id = ? GROUP BY kind",
                    (goal_id,),
                ).fetchall()
            )
            # cognition aggregates require unpacking payload_json
            cog_rows = self._db.execute(
                "SELECT payload_json FROM traces WHERE goal_id = ? AND kind = 'cognition'",
                (goal_id,),
            ).fetchall()
        latency_ms = 0
        tokens_in = 0
        tokens_out = 0
        for r in cog_rows:
            try:
                p = json.loads(r["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            latency_ms += int(p.get("latency_ms") or 0)
            tokens_in += int(p.get("tokens_in_est") or 0)
            tokens_out += int(p.get("tokens_out_est") or 0)
        return {
            "events_by_kind": {k: int(v) for k, v in counts.items()},
            "cognition_total_latency_ms": latency_ms,
            "cognition_tokens_in_est": tokens_in,
            "cognition_tokens_out_est": tokens_out,
        }

    def list_events(
        self,
        *,
        program_id: Optional[str] = None,
        task_id: Optional[str] = None,
        since_id: Optional[int] = None,
        limit: int = 500,
    ) -> list[TaskEvent]:
        """List events for a program or task in id (emission) order. Pass
        ``since_id`` to resume after a known cursor (exclusive)."""
        where: list[str] = []
        args: list[object] = []
        if program_id:
            where.append("program_id = ?")
            args.append(program_id)
        if task_id:
            where.append("task_id = ?")
            args.append(task_id)
        if not where:
            raise ValueError("list_events requires program_id or task_id")
        if since_id is not None:
            where.append("id > ?")
            args.append(since_id)
        args.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id ASC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    # ---- scheduling / recovery ------------------------------------------

    def count_running(self) -> int:
        """Global count of tasks currently 'running' — the in-flight count.
        Single-writer + recover-on-startup means every 'running' row really is
        in flight in this process, so concurrency caps derive straight from it."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
            ).fetchone()
        return int(row["n"])

    def list_pending_standalone(self, *, limit: int = 100) -> list[Task]:
        """Pending tasks with no program (the standalone path), oldest first so
        a backlog drains in submission order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE program_id IS NULL AND status = 'pending' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_nonterminal_programs(self) -> list[Program]:
        """Programs still in flight ('planning' or 'running') — what the
        reconcile pass and startup recovery walk."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM programs WHERE status IN ('planning', 'running') "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_program(r) for r in rows]

    def has_active_work(self) -> bool:
        """Cheap-idle guard: True iff anything needs scheduling. One COUNT each
        so an idle tick costs ~nothing (don't spend the engine on empty ticks)."""
        with self._lock:
            prog = self._db.execute(
                "SELECT 1 FROM programs WHERE status IN ('planning', 'running') LIMIT 1"
            ).fetchone()
            if prog:
                return True
            task = self._db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('pending', 'running') LIMIT 1"
            ).fetchone()
        return task is not None

    def reset_running_to_pending(self) -> list[str]:
        """Crash recovery — call ONCE at startup, before any scheduling. A task
        left 'running' by a dead process has no live execution behind it, so
        reset it to 'pending' to be re-run. Returns the reaped task ids (for the
        audit log). Safe only when nothing is in flight in THIS process yet."""
        with self._lock:
            ids = [
                r["id"]
                for r in self._db.execute(
                    "SELECT id FROM tasks WHERE status = 'running'"
                ).fetchall()
            ]
            if ids:
                self._db.execute(
                    "UPDATE tasks SET status = 'pending', started_at = NULL "
                    "WHERE status = 'running'"
                )
                self._db.commit()
        return ids

    def requeue_task(self, task_id: str) -> bool:
        """Put a single in-flight task back to 'pending' (e.g. when paused for a
        quota limit rather than failed). Returns True if a running row was reset."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'pending', started_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (task_id,),
            )
            self._db.commit()
            return cur.rowcount > 0

    # ---- meta / global flags (the quota pause) ---------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
            self._db.commit()

    def set_global_pause(self, until_ms: int, reason: str) -> None:
        """Pause ALL dispatch until ``until_ms`` (epoch ms) — the whole OAuth quota
        is account-wide, so a limit on one task pauses everything. Persisted so a
        restart still honours it."""
        self.set_meta("pause_until_ms", str(int(until_ms)))
        self.set_meta("pause_reason", reason or "")

    def global_pause(self) -> tuple[int, str]:
        """Return (until_ms, reason). until_ms is 0 when no pause is set."""
        raw = self.get_meta("pause_until_ms")
        try:
            until = int(raw) if raw else 0
        except ValueError:
            until = 0
        return until, (self.get_meta("pause_reason") or "")

    def clear_global_pause(self) -> None:
        self.delete_meta("pause_until_ms")
        self.delete_meta("pause_reason")

    # ---- workspace circuit-breaker (per-workspace pause) -----------------

    def count_recent_task_failures(self, workspace_dir: str, since_ms: int) -> int:
        """Number of tasks that failed for one workspace since ``since_ms``.
        Used by the circuit-breaker to trip a per-workspace hold when a run of
        failures piles up in a short window (the 2026-07-02 quota-burn pattern:
        one broken workspace keeps re-attempting until Denys notices)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE workspace_dir = ? AND status = 'failed' "
                "AND completed_at IS NOT NULL AND completed_at >= ?",
                (workspace_dir, since_ms),
            ).fetchone()
        return int(row["n"])

    def set_workspace_break(
        self, workspace_dir: str, until_ms: int, reason: str
    ) -> None:
        """Hold dispatch for ONE workspace until ``until_ms`` (epoch ms). Sibling
        of the global quota pause but scoped — other workspaces keep running."""
        self.set_meta(
            f"workspace_break:{workspace_dir}",
            json.dumps({"until_ms": int(until_ms), "reason": reason or ""}),
        )

    def get_workspace_break(self, workspace_dir: str) -> tuple[int, str]:
        """Return (until_ms, reason). until_ms is 0 when no break is set."""
        raw = self.get_meta(f"workspace_break:{workspace_dir}")
        if not raw:
            return 0, ""
        try:
            data = json.loads(raw)
            return int(data.get("until_ms") or 0), str(data.get("reason") or "")
        except (ValueError, TypeError):
            return 0, ""

    def clear_workspace_break(self, workspace_dir: str) -> None:
        self.delete_meta(f"workspace_break:{workspace_dir}")

    def list_workspace_breaks(self) -> list[tuple[str, int, str]]:
        """All currently-recorded workspace breaks (may include expired ones —
        the caller filters). Read surface for observability + ops-agent."""
        prefix = "workspace_break:"
        with self._lock:
            rows = self._db.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()
        out: list[tuple[str, int, str]] = []
        for r in rows:
            ws = r["key"][len(prefix):]
            try:
                data = json.loads(r["value"])
                out.append((ws, int(data.get("until_ms") or 0), str(data.get("reason") or "")))
            except (ValueError, TypeError):
                continue
        return out

    # ---- trend-detector cooldowns (typed wrappers over set_meta/get_meta) -

    def set_trend_cooldown(self, scope: str, signal_id: str, until_ms_str: str) -> None:
        """Persist the cooldown for one (scope, signal) pair. ``until_ms_str``
        is epoch milliseconds as a string — same shape as ``pause_until_ms``,
        so the trend detector reuses the meta table instead of inventing a
        per-repo JSON file that would recreate the write-concurrency cliff
        WAL already solved."""
        self.set_meta(f"trend_cooldown:{scope}:{signal_id}", until_ms_str)

    def get_trend_cooldown(self, scope: str, signal_id: str) -> Optional[str]:
        """The cooldown for one (scope, signal) pair, or ``None`` if no
        cooldown was set / has been cleared."""
        return self.get_meta(f"trend_cooldown:{scope}:{signal_id}")

    def set_trend_fingerprint(self, scope: str, signal_id: str, fp: str) -> None:
        """Persist the fingerprint (identity hash of the situation) of the
        LAST successful fire for one (scope, signal) pair. Added 2026-07-03
        after audit found R2 firing 4 days consecutively on identical evidence
        because the time-cooldown expired without any new data. The detector
        now compares new fires against this fingerprint and suppresses when
        the story hasn't changed. Distinct from cooldown (which is a wall-
        clock timer); this is content identity."""
        self.set_meta(f"trend_fingerprint:{scope}:{signal_id}", fp)

    def get_trend_fingerprint(self, scope: str, signal_id: str) -> Optional[str]:
        """The last-fire fingerprint for one (scope, signal) pair. ``None``
        when the signal has never fired at that scope (fresh fire allowed)."""
        return self.get_meta(f"trend_fingerprint:{scope}:{signal_id}")

    def set_trend_bookmark(self, workspace_dir: str, sha: str) -> None:
        """Persist the trend detector's last-seen SHA for one workspace. This
        is the DETECTOR'S OWN namespace — distinct from any future
        engineer-brief bookmark, so D1/D2 advancing the detector's view of
        "what changed" can't interfere with the engineer's catch-up read."""
        self.set_meta(f"trend_bookmark:{workspace_dir}", sha)

    def get_trend_bookmark(self, workspace_dir: str) -> Optional[str]:
        """The trend detector's last-seen SHA for one workspace, or ``None``
        if unset (first observation — bookmark-aware signals seed-and-skip)."""
        return self.get_meta(f"trend_bookmark:{workspace_dir}")

    def append_note_to_pending_tasks(self, program_id: str, note: str) -> list[str]:
        """Append a steering note to every PENDING task of a program — work not
        yet started. Running/done tasks are left untouched (already handed to the
        engine). Returns the affected task ids."""
        with self._lock:
            ids = [
                r["id"]
                for r in self._db.execute(
                    "SELECT id FROM tasks WHERE program_id = ? AND status = 'pending'",
                    (program_id,),
                ).fetchall()
            ]
            if ids:
                self._db.execute(
                    "UPDATE tasks SET goal = goal || ? "
                    "WHERE program_id = ? AND status = 'pending'",
                    (note, program_id),
                )
                self._db.commit()
        return ids

    def close(self) -> None:
        with self._lock:
            self._db.close()
