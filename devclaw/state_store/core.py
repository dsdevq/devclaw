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
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .control import ControlPlaneMixin
from .problems import ProblemsMixin
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
    derive_failure_class,
)

#: The retry loop's terminal-escalation suffix ("… (failed after N attempts)") —
#: the one place the attempt count survives into what the store sees at settle
#: time, so the eval_outcomes projection parses it back out. Best-effort: fail-
#: fast paths (timeout, review crash, worker block) never carry it → NULL.
_ATTEMPTS_SUFFIX_RE = re.compile(r"\(failed after (\d+) attempts\)\s*$")

#: Raw error text is truncated to this many chars in eval_outcomes rows — the
#: full text stays on the task row; the projection only needs enough to read.
_EVAL_ERROR_MAX_CHARS = 500

# ---- retention (volume hygiene) ---------------------------------------------
# Production evidence: a live devclaw.db reached 402MB with 200k+ trace rows —
# telemetry must not outgrow the state it observes. Two append-only, high-volume
# logs are pruned on an identical schedule: `traces` (per-tick observability,
# 2026-07-15) and `events` (raw runner SDK events, one row per agent action —
# the highest-volume table after traces, bounded 2026-07-18). The heartbeat
# calls :meth:`maybe_prune_traces` + :meth:`maybe_prune_events` on its cheap
# path; both route through the same table-agnostic :meth:`_maybe_prune_table`
# core — pure SQLite (zero LLM calls) and batched so a first prune of a huge
# backlog can never wedge a tick.

#: Days of trace history to keep when ``DEVCLAW_TRACE_RETENTION_DAYS`` is unset.
TRACE_RETENTION_DAYS_DEFAULT = 30
#: Days of event history to keep when ``DEVCLAW_EVENTS_RETENTION_DAYS`` is unset.
EVENTS_RETENTION_DAYS_DEFAULT = 30
#: Max rows deleted per prune call — one bounded batch per heartbeat tick until
#: the backlog drains, so a 400MB first prune spreads across ticks.
TRACE_PRUNE_BATCH = 5000
#: A new prune cycle (per table) starts at most once per day (watermark in ``meta``).
_TRACE_PRUNE_INTERVAL_MS = 24 * 3600 * 1000
#: meta key holding the epoch-ms of the last COMPLETED (drained) trace prune cycle.
_TRACE_PRUNE_META_KEY = "trace_prune_last_ms"
#: meta key holding the epoch-ms of the last COMPLETED (drained) events prune cycle.
_EVENTS_PRUNE_META_KEY = "events_prune_last_ms"

# ---- VACUUM (reclaim disk the prunes free, 2026-07-18) ----------------------
# The retention prunes DELETE rows but SQLite never returns freed pages to the
# OS on its own — the .db file only ever grows, freed pages are merely reused.
# A periodic VACUUM rebuilds the file so the space the prunes reclaim actually
# comes back. VACUUM rewrites the whole DB (holds the write lock, needs free
# disk ~= file size), so it runs RARELY (weekly) and only when there's real
# reclaim to be had (freelist past a threshold) — never on a healthy DB. Pure
# SQLite, zero LLM, on the heartbeat cheap path beside the prunes.
#: A VACUUM runs at most once per week (watermark in ``meta``).
_VACUUM_INTERVAL_MS = 7 * 24 * 3600 * 1000
#: Only VACUUM when at least this many free pages are reclaimable — at the 4KB
#: default page size, ~40MB. Below it the rewrite cost isn't worth the reclaim.
_VACUUM_MIN_FREELIST_PAGES = 10_000
#: meta key holding the epoch-ms of the last VACUUM cycle CHECK (vacuumed or not).
_VACUUM_META_KEY = "vacuum_last_ms"


def _parse_retention_days(raw: Optional[str], default: int) -> int:
    """Parse a retention-days env value with the fail-safe semantics shared by
    every retention surface: unset/blank → ``default``; ``0``, a negative value,
    or anything unparseable → ``0`` (retention disabled, gracefully — a typo in
    an env var must never make a prune delete aggressively or crash the
    heartbeat). Callers pass ``os.environ.get("DEVCLAW_…")`` directly so the env
    read stays a literal the doc-sync test (test_env_vars_doc_sync.py) can see."""
    if raw is None or not raw.strip():
        return default
    try:
        days = int(raw.strip())
    except ValueError:
        return 0
    return days if days > 0 else 0


def trace_retention_days() -> int:
    """Trace retention in days from ``DEVCLAW_TRACE_RETENTION_DAYS`` (see
    :func:`_parse_retention_days`)."""
    return _parse_retention_days(
        os.environ.get("DEVCLAW_TRACE_RETENTION_DAYS"), TRACE_RETENTION_DAYS_DEFAULT
    )


def events_retention_days() -> int:
    """Event retention in days from ``DEVCLAW_EVENTS_RETENTION_DAYS`` (see
    :func:`_parse_retention_days`)."""
    return _parse_retention_days(
        os.environ.get("DEVCLAW_EVENTS_RETENTION_DAYS"), EVENTS_RETENTION_DAYS_DEFAULT
    )


# ---- DB-size alarm (loud, not silent, 2026-07-18) ---------------------------
# Retention + VACUUM keep a healthy devclaw.db small, but if something writes
# faster than it prunes (or a prune is misconfigured off), the file grows until
# the VPS disk fills and the whole loop wedges — SILENTLY, because nothing
# watches size. This converts that silent wedge into ONE loud owner ping when
# the .db crosses a threshold (and re-arms when it drops back under). Pure stat,
# zero LLM, on the heartbeat cheap path beside the prunes/VACUUM.
#: Default alert threshold in MB when ``DEVCLAW_DB_SIZE_ALERT_MB`` is unset. The
#: 2026-07 incident that motivated retention was 402MB; 2GB is a clear "this is
#: wrong" line well above any healthy steady state.
DB_SIZE_ALERT_MB_DEFAULT = 2000
#: meta flag: "1" once the owner has been pinged about the current over-threshold
#: episode — cleared when size drops back under, so each crossing pings once.
_DB_SIZE_ALERTED_META_KEY = "db_size_alerted"


def db_size_alert_bytes() -> int:
    """Alert threshold in BYTES from ``DEVCLAW_DB_SIZE_ALERT_MB``. Unset/blank →
    the 2000MB default; ``0``, negative, or unparseable → ``0`` (alarm disabled,
    gracefully — a typo must never crash the heartbeat). The env read is a
    literal so the doc-sync test (test_env_vars_doc_sync.py) sees it."""
    raw = os.environ.get("DEVCLAW_DB_SIZE_ALERT_MB")
    if raw is None or not raw.strip():
        mb = DB_SIZE_ALERT_MB_DEFAULT
    else:
        try:
            mb = int(raw.strip())
        except ValueError:
            return 0
    return mb * 1024 * 1024 if mb > 0 else 0


class StateStore(ControlPlaneMixin, ProblemsMixin):
    def __init__(self, db_path: str) -> None:
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        #: resolved path to the .db file, kept for VACUUM / on-disk size checks.
        self._db_path = str(Path(db_path).expanduser())
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

    @property
    def db_path(self) -> str:
        """Resolved path of the backing SQLite file — doubles as the durable
        identity of this devclaw instance (the sandbox owner-label seed)."""
        return self._db_path

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

                -- Raw runner SDK events (one row per agent action inside every
                -- task) — the highest-volume append-only log after traces. Rows
                -- are never mutated (append + a daily retention DELETE of rows
                -- older than DEVCLAW_EVENTS_RETENTION_DAYS — see
                -- maybe_prune_events). Read by get_events + the SSE layer, which
                -- uses the monotonic id as its resume cursor.
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
                -- causal chain of a tick can be replayed. Rows are never
                -- mutated (append + a daily retention DELETE of rows older
                -- than DEVCLAW_TRACE_RETENTION_DAYS — see maybe_prune_traces).
                -- Read by the get_trace MCP tool and the dashboard.
                CREATE TABLE IF NOT EXISTS traces (
                  id              INTEGER PRIMARY KEY AUTOINCREMENT,
                  trace_id        TEXT NOT NULL,
                  goal_id         TEXT NOT NULL,
                  kind            TEXT NOT NULL,
                  ts              INTEGER NOT NULL,
                  payload_json    TEXT NOT NULL
                );

                -- Self-observability: the deduplicated PROBLEMS catalog. One
                -- row per DISTINCT failure devclaw hits (fingerprinted by
                -- category + kind + normalized message), UPSERTed on recurrence
                -- so `count` grows while the table stays bounded — NOT a row per
                -- occurrence (the #250 lesson). Written ONLY by
                -- StateStore.record_problem (single writer), from the failure
                -- choke points. recovered_count vs terminal_count splits
                -- carried-past failures (a limit that auto-resumes) from
                -- terminal ones. The capture/dedup layer; the ranked report is
                -- a deliberate follow-up. See state_store/problems.py.
                -- Continuous-eval OUTCOME PROJECTION (ADR 0006): one row per
                -- settled evaluation sample. source='live' rows materialize
                -- inside the settle write itself (mark_done / mark_failed /
                -- mark_task_cancelled — the same single writer that owns task
                -- rows, sharing the settle commit, exactly-once); source=
                -- 'basket' rows land via `devclaw evals ingest` from
                -- measure_passrate report JSONs (idempotent on source +
                -- report_ref + ticket). failure_class is MECHANICAL string
                -- bucketing (rows.derive_failure_class) — never an LLM call.
                CREATE TABLE IF NOT EXISTS eval_outcomes (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source       TEXT NOT NULL CHECK (source IN ('live','basket')),
                    task_id      TEXT,              -- live: task uuid; basket: NULL ok
                    ticket       TEXT,              -- basket: basket ticket id; live: NULL
                    goal_id      TEXT,
                    program_id   TEXT,
                    kind         TEXT,              -- fix_bug | implement_feature | ...
                    workspace_dir TEXT,
                    status       TEXT NOT NULL,     -- done | failed | cancelled
                    verify_passed INTEGER,          -- 1/0, NULL = no gate ran
                    pr_url       TEXT,
                    attempts     INTEGER,
                    wall_ms      INTEGER,
                    failure_class TEXT,             -- short mechanical class, NULL when done
                    error        TEXT,              -- truncated (<=500 chars) raw error
                    report_ref   TEXT,              -- basket: report JSON filename
                    settled_at   INTEGER NOT NULL   -- epoch ms
                );

                CREATE TABLE IF NOT EXISTS problems (
                  fingerprint     TEXT PRIMARY KEY,
                  category        TEXT NOT NULL,
                  kind            TEXT NOT NULL DEFAULT '',
                  summary         TEXT NOT NULL DEFAULT '',
                  sample_message  TEXT NOT NULL DEFAULT '',
                  count           INTEGER NOT NULL DEFAULT 0,
                  recovered_count INTEGER NOT NULL DEFAULT 0,
                  terminal_count  INTEGER NOT NULL DEFAULT 0,
                  first_seen_ms   INTEGER NOT NULL,
                  last_seen_ms    INTEGER NOT NULL,
                  last_goal_id    TEXT NOT NULL DEFAULT '',
                  last_task_id    TEXT NOT NULL DEFAULT ''
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
                # Planner-key of the PlannedTask this program-child row came
                # from (ADR 0003 stage 2) — for one-shot goals the key IS the
                # checklist item id, so the settle path can grade each item by
                # its own child task instead of the aggregate program verdict.
                # Null for standalone tasks and pre-existing rows.
                "ALTER TABLE tasks ADD COLUMN plan_key TEXT",
                # Durable gate-baseline (2026-07-19) — the pre-run HEAD captured
                # at the task's FIRST attempt. A pause→requeue re-run must diff
                # against THIS, not re-capture HEAD: by resume time HEAD is the
                # wip snapshot commit — the half-done work itself, not the base
                # (closeloop-bench b6d53bbd). Null for rows that predate the
                # column or never ran.
                "ALTER TABLE tasks ADD COLUMN pre_run_sha TEXT",
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
                CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_traces_goal      ON traces(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_trace     ON traces(trace_id, id);
                CREATE INDEX IF NOT EXISTS idx_traces_kind      ON traces(kind, id);
                CREATE INDEX IF NOT EXISTS idx_traces_ts        ON traces(ts);
                CREATE INDEX IF NOT EXISTS idx_problems_category ON problems(category);
                CREATE INDEX IF NOT EXISTS idx_problems_count    ON problems(count);
                CREATE INDEX IF NOT EXISTS idx_eval_outcomes_settled
                    ON eval_outcomes(settled_at);
                -- Exactly-once belts: one projection row per live task settle
                -- (the settle UPDATE's rowcount guard is the primary defense;
                -- this makes a re-insert structurally impossible), and re-
                -- ingesting the same basket report is a no-op, not duplicates.
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_outcomes_live_task
                    ON eval_outcomes(task_id) WHERE source = 'live';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_outcomes_basket
                    ON eval_outcomes(source, report_ref, ticket) WHERE source = 'basket';
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
        plan_key: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks
                     (id, kind, status, workspace_dir, goal, notify_url, created_at,
                      program_id, depends_on, order_idx, milestone, verify_cmd, deliver,
                      title, parent_goal_id, scaffold, plan_key)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    plan_key,
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
            cur = self._db.execute(
                "UPDATE tasks SET status = 'done', result_json = ?, "
                "pr_url = COALESCE(?, pr_url), completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (result_json, pr_url, _now_ms(), task_id),
            )
            if cur.rowcount == 1:
                # eval_outcomes projection (ADR 0006): materialized inside the
                # settle's own commit, only when a row actually moved — a no-op
                # re-settle writes nothing (exactly-once).
                self._insert_live_outcome(task_id, status="done", result_json=result_json)
            self._commit()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (error, _now_ms(), task_id),
            )
            moved = cur.rowcount == 1
            if moved:
                # eval_outcomes projection — same commit as the settle itself.
                self._insert_live_outcome(task_id, status="failed", error=error)
            self._commit()
        # Observability: a task settling FAILED is a problem devclaw hit — record
        # it (deduped) at this single choke point so every failure site
        # (timeout, review-crash, pause-bound, all-attempts-exhausted) is
        # covered by one call. Only when a row actually moved, so a no-op
        # re-settle can't inflate the count. `kind` is the error's first line
        # (the normalized full message is the fingerprint). Best-effort inside
        # record_problem — never raises back into the settle.
        if moved:
            first_line = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
            self.record_problem(
                category="task_fail",
                kind=first_line[:120],
                message=error or "",
                recovered=False,
                task_id=task_id,
            )

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
            if cur.rowcount == 1:
                # eval_outcomes projection — same commit as the settle itself.
                self._insert_live_outcome(task_id, status="cancelled")
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

    # ---- eval outcomes (continuous-eval projection, ADR 0006) -----------

    def _insert_live_outcome(
        self,
        task_id: str,
        *,
        status: str,
        result_json: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Materialize the ``eval_outcomes`` projection row for a task that just
        settled. Called by mark_done/mark_failed/mark_task_cancelled INSIDE their
        lock, AFTER the settle UPDATE (so the row read back already carries the
        final pr_url/completed_at) and BEFORE their ``_commit`` — the insert
        shares the settle's commit, so a settle and its projection row are one
        atomic unit. Exactly-once: callers only invoke this when the settle
        UPDATE moved a row, and the partial unique index on (task_id) makes a
        duplicate structurally an IGNORE.

        Everything is derived from what the store already knows at settle time,
        mechanically (zero LLM):
          * ``verify_passed`` — the result's verify block (done), or 0 when the
            error buckets as ``verify_failed``; NULL = no gate produced a verdict;
          * ``failure_class`` — :func:`rows.derive_failure_class` string bucketing;
          * ``attempts`` — parsed from the retry loop's terminal "(failed after
            N attempts)" suffix; fail-fast paths carry no count → NULL;
          * ``wall_ms`` — completed_at − started_at from the row itself.

        Best-effort: a projection hiccup logs and is dropped — it must never
        unsettle the task (the settle UPDATE still commits)."""
        try:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return
            verify_passed: Optional[int] = None
            if result_json:
                try:
                    verify = (json.loads(result_json) or {}).get("verify") or {}
                    if verify.get("ran"):
                        verify_passed = 1 if verify.get("passed") else 0
                except (TypeError, ValueError):
                    pass
            failure_class: Optional[str] = None
            if status == "failed":
                failure_class = derive_failure_class(error)
                if failure_class == "verify_failed" and verify_passed is None:
                    verify_passed = 0
            attempts: Optional[int] = None
            if error:
                m = _ATTEMPTS_SUFFIX_RE.search(error)
                if m:
                    attempts = int(m.group(1))
            completed, started = row["completed_at"], row["started_at"]
            wall_ms = (completed - started) if (completed and started) else None
            self._db.execute(
                "INSERT OR IGNORE INTO eval_outcomes "
                "(source, task_id, goal_id, program_id, kind, workspace_dir, "
                " status, verify_passed, pr_url, attempts, wall_ms, "
                " failure_class, error, settled_at) "
                "VALUES ('live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    row["parent_goal_id"],
                    row["program_id"],
                    row["kind"],
                    row["workspace_dir"],
                    status,
                    verify_passed,
                    row["pr_url"],
                    attempts,
                    wall_ms,
                    failure_class,
                    (error or "")[:_EVAL_ERROR_MAX_CHARS] or None,
                    completed if completed else _now_ms(),
                ),
            )
        except Exception as err:  # noqa: BLE001 — telemetry must never unsettle
            sys.stderr.write(
                f"state-store: eval_outcomes projection failed task={task_id}: {err}\n"
            )

    def record_basket_outcome(
        self,
        *,
        report_ref: str,
        ticket: str,
        status: str,
        task_id: Optional[str] = None,
        kind: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        verify_passed: Optional[bool] = None,
        pr_url: Optional[str] = None,
        wall_ms: Optional[int] = None,
        error: Optional[str] = None,
        settled_at: Optional[int] = None,
    ) -> bool:
        """Insert one ``source='basket'`` eval_outcomes row from a
        measure_passrate report record. Idempotent on (source, report_ref,
        ticket) via the partial unique index — re-ingesting the same report is
        a no-op. Returns True iff a NEW row was inserted. ``failure_class`` is
        derived here with the same mechanical bucketing live rows use."""
        failure_class = derive_failure_class(error) if status == "failed" else None
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO eval_outcomes "
                "(source, task_id, ticket, kind, workspace_dir, status, "
                " verify_passed, pr_url, wall_ms, failure_class, error, "
                " report_ref, settled_at) "
                "VALUES ('basket', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    ticket,
                    kind,
                    workspace_dir,
                    status,
                    None if verify_passed is None else (1 if verify_passed else 0),
                    pr_url,
                    wall_ms,
                    failure_class,
                    (error or "")[:_EVAL_ERROR_MAX_CHARS] or None,
                    report_ref,
                    settled_at if settled_at is not None else _now_ms(),
                ),
            )
            self._commit()
            return cur.rowcount == 1

    def list_eval_outcomes(
        self, *, source: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """Recent eval_outcomes rows, newest settle first — the read surface
        the console/night-report layers (PR2/PR3) and tests project from.
        Plain dicts, pure SELECT."""
        where = "WHERE source = ?" if source else ""
        args: tuple = (source, limit) if source else (limit,)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM eval_outcomes {where} "
                "ORDER BY settled_at DESC, id DESC LIMIT ?",
                args,
            ).fetchall()
        return [dict(r) for r in rows]

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
        goal_id: Optional[str] = None,
        since_id: int = 0,
        limit: int = 500,
        kind: Optional[str] = None,
        role: Optional[str] = None,
        since_ms: Optional[int] = None,
        errors_only: bool = False,
        newest_first: bool = False,
    ) -> list[dict]:
        """Read trace events in emission order. Pure SELECT — every filter is
        applied in SQL (the production table holds 200k+ rows; loading-then-
        filtering in Python is not an option). ``goal_id``/``kind`` ride their
        indexes; ``since_ms`` rides ``idx_traces_ts``; ``role`` (cognition
        payload field) and ``errors_only`` (non-empty ``error`` payload field)
        use ``json_extract`` over the already-narrowed row set.

        Pass ``since_id`` to resume after a known cursor (exclusive);
        ``newest_first=True`` flips the ordering to ``id DESC`` so "the last N
        matching events" is one indexed query, not a full-table read."""
        sql = (
            "SELECT id, trace_id, goal_id, kind, ts, payload_json FROM traces "
            "WHERE id > ?"
        )
        args: list[object] = [since_id]
        if goal_id:
            sql += " AND goal_id = ?"
            args.append(goal_id)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if since_ms is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ms))
        if role:
            sql += " AND json_extract(payload_json, '$.role') = ?"
            args.append(role)
        if errors_only:
            sql += " AND COALESCE(json_extract(payload_json, '$.error'), '') != ''"
        sql += f" ORDER BY id {'DESC' if newest_first else 'ASC'} LIMIT ?"
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

    def _prune_table_batch(self, *, table: str, older_than_ms: int, limit: int) -> int:
        """Delete up to ``limit`` of the OLDEST rows in ``table`` with ``ts``
        before ``older_than_ms``. Returns the number of rows deleted. One
        bounded batch — the caller loops across heartbeat ticks (via
        :meth:`_maybe_prune_table`) rather than holding the write lock long
        enough to wedge a tick on a 200k-row backlog.

        ``table`` is a fixed module-controlled literal (``traces`` / ``events``),
        never user input, and both tables share the ``id`` PK + monotonic ``ts``
        shape this query relies on (ordering by ``id`` reads the oldest rows off
        the front of the PK)."""
        with self._lock:
            cur = self._db.execute(
                f"DELETE FROM {table} WHERE id IN ("  # noqa: S608 — fixed literal, not user input
                f"SELECT id FROM {table} WHERE ts < ? ORDER BY id ASC LIMIT ?)",
                (older_than_ms, limit),
            )
            self._commit()
            return int(cur.rowcount)

    def _maybe_prune_table(
        self,
        *,
        table: str,
        meta_key: str,
        retention_days: int,
        now_ms: int,
        batch_limit: int,
    ) -> int:
        """Table-agnostic retention prune — the shared core behind
        :meth:`maybe_prune_traces` and :meth:`maybe_prune_events` (StateStore
        owns both logs' writes, so the prune lives here beside them, not in a
        second writer).

        Semantics:
          * disabled (``retention_days`` <= 0) → no-op, returns 0;
          * a new prune CYCLE starts at most once per ``_TRACE_PRUNE_INTERVAL_MS``
            (daily), gated by the per-table ``meta_key`` watermark;
          * each call deletes at most ``batch_limit`` rows; the watermark is
            advanced only when a batch comes back short (backlog drained), so
            an oversized first prune drains one bounded batch per tick instead
            of blocking a single tick for the whole 400MB table.

        Pure SQLite — zero LLM calls, safe on the zero-token idle path."""
        if retention_days <= 0:
            return 0
        raw = self.get_meta(meta_key)
        try:
            last = int(raw) if raw else 0
        except ValueError:
            last = 0
        if last and (now_ms - last) < _TRACE_PRUNE_INTERVAL_MS:
            return 0
        deleted = self._prune_table_batch(
            table=table, older_than_ms=now_ms - retention_days * 24 * 3600 * 1000,
            limit=batch_limit,
        )
        if deleted < batch_limit:
            # Drained — stamp the watermark so the next cycle waits a day.
            # A full batch leaves the watermark alone: more rows may remain,
            # and the next tick continues the drain.
            self.set_meta(meta_key, str(now_ms))
        return deleted

    def prune_trace_batch(
        self, *, older_than_ms: int, limit: int = TRACE_PRUNE_BATCH
    ) -> int:
        """One bounded batch of the traces retention prune. See
        :meth:`_prune_table_batch`."""
        return self._prune_table_batch(
            table="traces", older_than_ms=older_than_ms, limit=limit,
        )

    def maybe_prune_traces(
        self,
        *,
        now_ms: Optional[int] = None,
        retention_days: Optional[int] = None,
        batch_limit: int = TRACE_PRUNE_BATCH,
    ) -> int:
        """Retention prune for the traces table — the heartbeat's cheap-path
        maintenance hook. Thin wrapper over :meth:`_maybe_prune_table`."""
        days = trace_retention_days() if retention_days is None else retention_days
        now = _now_ms() if now_ms is None else now_ms
        return self._maybe_prune_table(
            table="traces", meta_key=_TRACE_PRUNE_META_KEY,
            retention_days=days, now_ms=now, batch_limit=batch_limit,
        )

    def maybe_prune_events(
        self,
        *,
        now_ms: Optional[int] = None,
        retention_days: Optional[int] = None,
        batch_limit: int = TRACE_PRUNE_BATCH,
    ) -> int:
        """Retention prune for the events table (raw runner SDK events, one row
        per agent action — the highest-volume append-only log after traces).
        The heartbeat's cheap-path maintenance hook, beside the trace prune.
        Thin wrapper over :meth:`_maybe_prune_table`."""
        days = events_retention_days() if retention_days is None else retention_days
        now = _now_ms() if now_ms is None else now_ms
        return self._maybe_prune_table(
            table="events", meta_key=_EVENTS_PRUNE_META_KEY,
            retention_days=days, now_ms=now, batch_limit=batch_limit,
        )

    def maybe_vacuum(
        self,
        *,
        now_ms: Optional[int] = None,
        interval_ms: int = _VACUUM_INTERVAL_MS,
        min_freelist_pages: int = _VACUUM_MIN_FREELIST_PAGES,
    ) -> bool:
        """Weekly VACUUM that returns the disk the retention prunes free back to
        the OS (SQLite reuses freed pages but never shrinks the .db file on its
        own). The heartbeat's cheap-path maintenance hook, beside the prunes.

        Semantics:
          * checked at most once per ``interval_ms`` (weekly), gated by the
            ``vacuum_last_ms`` meta watermark — stamped on every CHECK (whether
            or not it vacuumed) so a healthy DB isn't re-inspected every tick;
          * only actually VACUUMs when the freelist is at least
            ``min_freelist_pages`` (real reclaim to be had) — a rewrite of a
            near-full DB for a few free pages isn't worth the write-lock cost;
          * never runs inside an open ``transaction()`` (VACUUM cannot run in a
            transaction) — defers to a later tick.

        Returns True iff it actually VACUUMed. Pure SQLite — zero LLM calls,
        safe on the zero-token idle path (the rare weekly rewrite aside)."""
        now = _now_ms() if now_ms is None else now_ms
        raw = self.get_meta(_VACUUM_META_KEY)
        try:
            last = int(raw) if raw else 0
        except ValueError:
            last = 0
        if last and (now - last) < interval_ms:
            return False
        with self._lock:
            if self._txn_depth > 0:
                # Mid atomic unit — VACUUM would raise. Try again next tick
                # (do NOT stamp the watermark: this cycle never happened).
                return False
            free = int(self._db.execute("PRAGMA freelist_count").fetchone()[0])
            if free < min_freelist_pages:
                # Inspected, nothing worth reclaiming — stamp so we wait a full
                # interval rather than re-checking every tick.
                self.set_meta(_VACUUM_META_KEY, str(now))
                return False
            self._db.commit()  # VACUUM requires no open transaction
            self._db.execute("VACUUM")
            self._db.commit()
            # Stamp only AFTER a successful rewrite: a VACUUM that raises (e.g.
            # not enough scratch disk — it needs ~file-size free) leaves the
            # watermark alone, so the next tick retries rather than deferring a
            # full week. Symmetric with the transaction-defer path above.
            self.set_meta(_VACUUM_META_KEY, str(now))
            return True

    def db_size_bytes(self) -> int:
        """On-disk size of the SQLite database, INCLUDING the WAL sidecar (an
        un-checkpointed WAL can itself be large). Best-effort: a missing file
        (e.g. an in-memory DB) counts as 0, never raises."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self._db_path + suffix)
            except OSError:
                pass
        return total

    def check_db_size_alert(
        self, *, threshold_bytes: Optional[int] = None, now_ms: Optional[int] = None
    ) -> Optional[str]:
        """One-shot DB-size alarm — the loud-not-silent guard against the .db
        quietly growing until the disk fills and the loop wedges.

        Returns a plain owner-facing message the FIRST tick the file crosses
        ``threshold_bytes`` (deduped via the ``db_size_alerted`` meta flag), and
        ``None`` on every tick after that until the size drops back under, which
        RE-ARMS the alarm (clears the flag) so a later re-crossing pings again.
        Disabled (``threshold_bytes`` resolves to 0) → always ``None``.

        Pure stat + a meta read/write — zero LLM, safe on the zero-token idle
        path. The ``now_ms`` arg is accepted for signature symmetry with the
        prunes/VACUUM and to keep the message deterministic in tests."""
        threshold = db_size_alert_bytes() if threshold_bytes is None else threshold_bytes
        if threshold <= 0:
            return None
        size = self.db_size_bytes()
        alerted = self.get_meta(_DB_SIZE_ALERTED_META_KEY) == "1"
        if size < threshold:
            if alerted:
                self.delete_meta(_DB_SIZE_ALERTED_META_KEY)  # re-arm for next crossing
            return None
        if alerted:
            return None  # already pinged for this episode
        self.set_meta(_DB_SIZE_ALERTED_META_KEY, "1")
        gb = size / (1024 * 1024 * 1024)
        thr_gb = threshold / (1024 * 1024 * 1024)
        return (
            f"⚠️ devclaw.db has grown to {gb:.2f} GB (alarm threshold "
            f"{thr_gb:.2f} GB). Retention/VACUUM may be falling behind, a "
            f"retention env var may be disabled, or something is writing faster "
            f"than it prunes. Check the VPS disk and DEVCLAW_*_RETENTION_DAYS."
        )

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

    def set_task_pre_run_sha(self, task_id: str, sha: str) -> None:
        """Persist the gate-baseline sha captured at the task's first run.
        Written once by the queue (single-writer) before the attempt loop; a
        pause→requeue re-run reads it back instead of re-capturing HEAD (which
        by then is the wip snapshot commit — the work itself, not the base)."""
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET pre_run_sha = ? WHERE id = ?", (sha, task_id)
            )
            self._commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
