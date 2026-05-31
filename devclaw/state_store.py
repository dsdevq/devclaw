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
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

TaskStatus = Literal["pending", "running", "done", "failed"]
TaskKind = Literal["implement_feature", "fix_bug", "review_repository"]
# Programs hold a DAG of tasks decomposed from a single high-level goal.
#   planning — planner still decomposing (claude subprocess in flight)
#   running  — tasks exist, none failed, not all terminal yet
#   done     — every task is 'done'
#   failed   — planner failed OR any task failed (sticky; siblings are not
#              scheduled after a failure — see TaskQueue for the policy)
ProgramStatus = Literal["planning", "running", "done", "failed"]


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


class StateStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")  # concurrent reads, single writer
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
                  order_idx       INTEGER
                );

                CREATE TABLE IF NOT EXISTS programs (
                  id              TEXT PRIMARY KEY,
                  goal            TEXT NOT NULL,
                  workspace_dir   TEXT NOT NULL,
                  notify_url      TEXT,
                  status          TEXT NOT NULL,
                  error           TEXT,
                  created_at      INTEGER NOT NULL,
                  completed_at    INTEGER
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
                CREATE INDEX IF NOT EXISTS idx_programs_status  ON programs(status);
                CREATE INDEX IF NOT EXISTS idx_events_program   ON events(program_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_task      ON events(task_id, id);
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
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks
                     (id, kind, status, workspace_dir, goal, notify_url, created_at,
                      program_id, depends_on, order_idx)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
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

    def mark_done(self, task_id: str, result_json: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'done', result_json = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (result_json, _now_ms(), task_id),
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
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO programs (id, goal, workspace_dir, notify_url, status, created_at) "
                "VALUES (?, ?, ?, ?, 'planning', ?)",
                (id, goal, workspace_dir, notify_url, _now_ms()),
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

    def close(self) -> None:
        with self._lock:
            self._db.close()
