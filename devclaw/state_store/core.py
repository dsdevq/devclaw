"""SQLite state store for DevClaw tasks — the core append-only event log +
single-writer engine.

Tracks every task DevClaw has been asked to run, its current status, and the
result (or error) once it terminates. ``sqlite3`` is sync; a re-entrant lock
serializes access because FastMCP may touch the store from the event loop and
from background tasks. WAL mode gives concurrent reads with a single writer.

The thin typed ``meta`` wrappers (quota pause, operator hold, run windows,
workspace breaker, trend cooldowns) live on :class:`ControlPlaneMixin` in
``control.py``; the pure data (dataclasses + row mappers + literals) lives in
``rows.py``. This module holds the connection, the transaction machinery, and
the task/program/event/trace CRUD.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .control import ControlPlaneMixin
from .rows import (
    SQLITE_BUSY_TIMEOUT_MS,
    Program,
    Task,
    TaskEvent,
    TaskKind,
    TaskStatus,
    _now_ms,
    _row_to_event,
    _row_to_program,
    _row_to_task,
)


class StateStore(ControlPlaneMixin):
    def __init__(self, db_path: str) -> None:
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")  # concurrent reads, single writer
        self._db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")  # wait, don't fail-fast
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        #: transaction()-nesting depth. 0 == no open transaction, so _commit()
        #: writes immediately (every existing single-write method behaves exactly
        #: as before). > 0 == inside a transaction(): _commit() is a no-op and the
        #: OUTERMOST transaction() issues the single real commit/rollback.
        self._txn_depth = 0
        #: set True when any exception passes through an open transaction() level,
        #: so the outermost level rolls the whole unit back — even if an inner
        #: exception was caught between nested levels.
        self._txn_failed = False
        self._bootstrap()

    # ---- transactions ---------------------------------------------------
    #
    # Group several writes into ONE atomic unit spanning multiple store methods
    # (e.g. "create a task row AND stamp the goal's in_flight ref" — the
    # atomicity a later dispatch/orphan-recovery PR needs). Single writes called
    # OUTSIDE a transaction() keep committing immediately, unchanged.

    @contextmanager
    def transaction(self) -> "Iterator[StateStore]":
        """Open an atomic unit. Acquires the store lock for the WHOLE block (so
        no other thread can commit or write while it is open), and defers the
        commit until the OUTERMOST ``transaction()`` exits — nested
        ``transaction()`` calls join the outer one (a depth counter), yielding a
        single commit at depth 0. Any exception at any depth rolls the whole
        unit back.

        Existing single-write methods call :meth:`_commit`, which is a no-op
        while a transaction is open, so a ``create_task`` (or any other write)
        run inside ``transaction()`` becomes part of the atomic unit instead of
        committing on its own.
        """
        with self._lock:
            if self._txn_depth == 0:
                self._txn_failed = False
            self._txn_depth += 1
            try:
                yield self
            except BaseException:
                self._txn_failed = True
                raise
            finally:
                self._txn_depth -= 1
                if self._txn_depth == 0:
                    if self._txn_failed:
                        self._db.rollback()
                    else:
                        self._db.commit()
                    self._txn_failed = False

    def _commit(self) -> None:
        """Commit now, unless a :meth:`transaction` is open. Inside a
        transaction (depth > 0) this is a no-op — the write joins the atomic
        unit and the outermost ``transaction()`` commits once. Outside one
        (depth 0) it commits immediately, so every single-write method keeps its
        original commit-per-call behavior."""
        if self._txn_depth == 0:
            self._db.commit()

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
                  parent_goal_id  TEXT,
                  pause_count     INTEGER NOT NULL DEFAULT 0,
                  scaffold        INTEGER NOT NULL DEFAULT 0
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
                  verify_cmd      TEXT,
                  parent_goal_id  TEXT
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
                # Durable goal-owner pointer on PROGRAMS too (2026-07-10) —
                # the only recovery path when the goal-side in_flight ref is
                # lost (STATUS.md truncated by a crash mid-write).
                "ALTER TABLE programs ADD COLUMN parent_goal_id TEXT",
                # Usage-limit requeue counter (2026-07-10) — bounds the
                # pause→requeue→re-run loop (see Task.pause_count).
                "ALTER TABLE tasks ADD COLUMN pause_count INTEGER NOT NULL DEFAULT 0",
                # Generated-scaffolding flag (L3, #222) — a scaffold task skips
                # ONLY the adversarial review gate (verify gate + test-integrity
                # still run). Defaulted so pre-existing rows read as non-scaffold.
                "ALTER TABLE tasks ADD COLUMN scaffold INTEGER NOT NULL DEFAULT 0",
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
                CREATE INDEX IF NOT EXISTS idx_programs_parent_goal ON programs(parent_goal_id);
                CREATE INDEX IF NOT EXISTS idx_events_program   ON events(program_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_task      ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_goal      ON traces(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_trace     ON traces(trace_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_kind      ON traces(kind, id);
                """
            )
            self._commit()

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
        scaffold: bool = False,
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks
                     (id, kind, status, workspace_dir, goal, notify_url, created_at,
                      program_id, depends_on, order_idx, milestone, verify_cmd, deliver,
                      title, parent_goal_id, scaffold)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    1 if scaffold else 0,
                ),
            )
            self._commit()

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_now_ms(), task_id),
            )
            self._commit()

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
            self._commit()
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
            self._commit()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (error, _now_ms(), task_id),
            )
            self._commit()

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
            self._commit()
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
                self._commit()
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
        parent_goal_id: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        parent_goal_id_is_null: bool = False,
        limit: int = 100,
    ) -> list[Task]:
        """Recent tasks, newest first. Extra filters:

        - ``parent_goal_id`` — only tasks owned by this goal (GoalDetail's
          Dispatched Tasks section).
        - ``workspace_dir`` — tasks whose workspace matches (ProjectDetail
          Recent Tasks strip).
        - ``parent_goal_id_is_null`` — restrict to standalone tasks (no goal
          owns them). Combine with workspace_dir to get the "loose tasks in
          this project" set — avoids double-counting tasks already visible
          inside a goal.
        """
        where: list[str] = []
        args: list[object] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if parent_goal_id is not None:
            where.append("parent_goal_id = ?")
            args.append(parent_goal_id)
        if parent_goal_id_is_null:
            where.append("parent_goal_id IS NULL")
        if workspace_dir is not None:
            where.append("workspace_dir = ?")
            args.append(workspace_dir)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks {where_sql} ORDER BY created_at DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def latest_task_for_goal(self, goal_id: str) -> Optional[Task]:
        """The most recent task dispatched by ``goal_id`` (any status), or None.
        Mirrors :meth:`latest_program_for_goal`; a later orphan-recovery pass
        reads it to re-adopt a task whose goal-side in_flight ref was lost."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE parent_goal_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
        return _row_to_task(row) if row else None

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
        parent_goal_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO programs "
                "(id, goal, workspace_dir, notify_url, status, created_at, open_pr, "
                " verify_cmd, parent_goal_id) "
                "VALUES (?, ?, ?, ?, 'planning', ?, ?, ?, ?)",
                (
                    id, goal, workspace_dir, notify_url, _now_ms(),
                    1 if open_pr else 0, verify_cmd, parent_goal_id,
                ),
            )
            self._commit()

    def mark_program_running(self, program_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'running' "
                "WHERE id = ? AND status = 'planning'",
                (program_id,),
            )
            self._commit()

    def mark_program_done(self, program_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'done', completed_at = ? "
                "WHERE id = ? AND status IN ('planning', 'running')",
                (_now_ms(), program_id),
            )
            self._commit()

    def mark_program_failed(self, program_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE programs SET status = 'failed', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('planning', 'running')",
                (error, _now_ms(), program_id),
            )
            self._commit()

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
            self._commit()

    def list_programs(self, *, limit: int = 100) -> list[Program]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM programs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_program(r) for r in rows]

    def latest_program_for_goal(self, goal_id: str) -> Optional[Program]:
        """The most recent program dispatched by ``goal_id`` (any status).
        Read by the goal layer's startup orphan sweep: if this program's
        result never made it into the goal's log, the goal re-adopts it."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM programs WHERE parent_goal_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
        return _row_to_program(row) if row else None

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
            self._commit()
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
            self._commit()
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
        cognition total latency, tokens, and cost. Cheap SQL — no LLM call.

        Token totals prefer REAL usage (recorded from the CLI's json envelope
        since T0.5) per row; rows without it (legacy rows, raw-stdout fallback)
        contribute their len/4 estimate — ``cognition_rows_estimated`` says how
        many rows in the total are estimates. The pure-estimate ``*_est`` sums
        are kept for back-compat."""
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
        tokens_in_est = 0
        tokens_out_est = 0
        rows_with_real = 0
        rows_estimated = 0
        cost_usd = 0.0
        for r in cog_rows:
            try:
                p = json.loads(r["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            latency_ms += int(p.get("latency_ms") or 0)
            tokens_in_est += int(p.get("tokens_in_est") or 0)
            tokens_out_est += int(p.get("tokens_out_est") or 0)
            real_in, real_out = p.get("tokens_in"), p.get("tokens_out")
            if real_in is not None or real_out is not None:
                rows_with_real += 1
                tokens_in += int(real_in or 0)
                tokens_out += int(real_out or 0)
            else:
                rows_estimated += 1
                tokens_in += int(p.get("tokens_in_est") or 0)
                tokens_out += int(p.get("tokens_out_est") or 0)
            c = p.get("cost_usd")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                cost_usd += float(c)
        return {
            "events_by_kind": {k: int(v) for k, v in counts.items()},
            "cognition_total_latency_ms": latency_ms,
            "cognition_tokens_in": tokens_in,
            "cognition_tokens_out": tokens_out,
            "cognition_rows_with_real_usage": rows_with_real,
            "cognition_rows_estimated": rows_estimated,
            "cognition_cost_usd": round(cost_usd, 6),
            "cognition_tokens_in_est": tokens_in_est,
            "cognition_tokens_out_est": tokens_out_est,
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
                self._commit()
        return ids

    def requeue_task(self, task_id: str) -> bool:
        """Put a single in-flight task back to 'pending' (when paused for a
        quota limit rather than failed). Increments ``pause_count`` in the same
        statement so the pause→requeue loop is countable (and thus boundable) —
        read it back via :meth:`get_task`. Returns True if a running row was
        reset."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'pending', started_at = NULL, "
                "pause_count = pause_count + 1 "
                "WHERE id = ? AND status = 'running'",
                (task_id,),
            )
            self._commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
