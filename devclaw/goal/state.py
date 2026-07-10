"""Tranche 1 substrate — the SQLite home for goal state (tables only, UNUSED).

Goal state today is spread across per-goal files under ``DEVCLAW_GOALS_DIR``
(``goal.yaml`` / ``STATUS.md`` / ``log.md`` / ``inbox.md`` / ``deliveries.md`` …)
and linked to the task queue only by a string goal id. The approved Tranche 1
plan consolidates that state into the SAME ``devclaw.db`` SQLite database that
:class:`devclaw.state_store.StateStore` already owns (WAL, one shared
connection guarded by a single ``threading.RLock``), so a task row and its
owning goal's state can be written in one atomic :meth:`StateStore.transaction`.

**This module is the foundation ONLY.** ``GoalState`` idempotently creates the
goal-state tables on construction and nothing more — no read/write methods, and
nothing in the running system reads or writes these tables yet. Later PRs (not
this one) migrate status, phase history, steering, log, deliveries, settlements,
and docs off the filesystem and onto these tables. Until then the tables are
created-but-empty: pure substrate, zero behavior change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state_store import StateStore


class GoalState:
    """Owns the goal-state tables inside a shared :class:`StateStore`.

    Handed a ``StateStore``, it borrows that store's single sqlite connection,
    its ``RLock``, and its ``transaction()`` seam — so goal-state writes land in
    the same database and can join the same atomic unit as task/program writes.
    Construction bootstraps the tables idempotently (``CREATE TABLE IF NOT
    EXISTS``), mirroring the store's own ``_bootstrap`` style. No other methods
    yet — the tables stay unused until later Tranche 1 PRs migrate onto them.
    """

    def __init__(self, store: "StateStore") -> None:
        self._store = store
        self._bootstrap()

    def _bootstrap(self) -> None:
        # Idempotent — safe to run on every construction (matches
        # StateStore._bootstrap). Uses the shared connection + lock; commits via
        # the store's _commit(), a no-op inside an open transaction().
        with self._store._lock:
            self._store._db.executescript(
                """
                -- One row per goal: the machine state STATUS.md holds in YAML
                -- frontmatter today. `version` is an optimistic-concurrency
                -- counter a later migration bumps on each write; in_flight_*
                -- carry the durable pointer to the goal's running task/program.
                CREATE TABLE IF NOT EXISTS goal_status (
                  goal_id               TEXT PRIMARY KEY,
                  version               INTEGER NOT NULL DEFAULT 0,
                  state                 TEXT,
                  blocked_on            TEXT,
                  next                  TEXT,
                  last_plan_at          TEXT,
                  last_tick_at          TEXT,
                  actions_dispatched    INTEGER,
                  deliveries_since_eval INTEGER,
                  last_eval_verdict     TEXT,
                  last_eval_at          TEXT,
                  last_eval_note        TEXT,
                  last_progress_at      TEXT,
                  no_progress_notified  INTEGER,
                  in_flight_ref_id      TEXT,
                  in_flight_kind        TEXT,
                  in_flight_json        TEXT,
                  inbox_ingest_cursor   INTEGER NOT NULL DEFAULT 0,
                  updated_at            INTEGER
                );

                -- Append-only phase transitions (STATUS.md phase_history today).
                CREATE TABLE IF NOT EXISTS goal_phase_history (
                  id         INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id    TEXT NOT NULL,
                  phase      TEXT NOT NULL,
                  at         TEXT NOT NULL
                );

                -- Steering lines (inbox.md today). consumed_at NULL == unread;
                -- a later migration stamps it when the tick ingests the line.
                CREATE TABLE IF NOT EXISTS goal_steering (
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id     TEXT NOT NULL,
                  source      TEXT NOT NULL,
                  line        TEXT NOT NULL,
                  created_at  INTEGER NOT NULL,
                  consumed_at INTEGER
                );

                -- Append-only event log (log.md today).
                CREATE TABLE IF NOT EXISTS goal_log (
                  id       INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id  TEXT NOT NULL,
                  ts       INTEGER NOT NULL,
                  message  TEXT NOT NULL
                );

                -- Grounded record of what each action shipped (deliveries.md
                -- today). UNIQUE(goal_id, ref_id) makes ingest idempotent.
                CREATE TABLE IF NOT EXISTS goal_deliveries (
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id     TEXT NOT NULL,
                  ref_id      TEXT NOT NULL,
                  instruction TEXT,
                  body        TEXT,
                  created_at  INTEGER NOT NULL,
                  UNIQUE(goal_id, ref_id)
                );

                -- One row per settled in-flight ref — the dedupe key a later
                -- settle/reconcile pass uses to tell "settled and recorded"
                -- from "the in_flight ref was lost before the result was seen".
                CREATE TABLE IF NOT EXISTS goal_settlements (
                  goal_id    TEXT NOT NULL,
                  ref_id     TEXT NOT NULL,
                  ref_kind   TEXT,
                  status     TEXT,
                  settled_at INTEGER,
                  UNIQUE(goal_id, ref_id)
                );

                -- Free-form per-goal documents keyed by kind (spec / discovery /
                -- checklist / firmed-draft … the *.md and *.yaml artifacts
                -- today). PRIMARY KEY(goal_id, kind) == one current doc per kind.
                CREATE TABLE IF NOT EXISTS goal_docs (
                  goal_id    TEXT NOT NULL,
                  kind       TEXT NOT NULL,
                  content    TEXT,
                  updated_at INTEGER,
                  PRIMARY KEY(goal_id, kind)
                );

                -- goal_id lookups on the append-only / multi-row tables.
                CREATE INDEX IF NOT EXISTS idx_goal_phase_history_goal
                  ON goal_phase_history(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_goal_steering_goal
                  ON goal_steering(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_goal_log_goal
                  ON goal_log(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_goal_deliveries_goal
                  ON goal_deliveries(goal_id, id);
                CREATE INDEX IF NOT EXISTS idx_goal_settlements_goal
                  ON goal_settlements(goal_id, ref_id);
                """
            )
            self._store._commit()
