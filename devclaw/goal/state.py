"""Tranche 1 substrate — the SQLite home for goal state.

Goal state used to be spread across per-goal files under ``DEVCLAW_GOALS_DIR``
(``goal.yaml`` / ``STATUS.md`` / ``log.md`` / ``inbox.md`` / ``deliveries.md`` …)
and linked to the task queue only by a string goal id. The approved Tranche 1
plan consolidates that state into the SAME ``devclaw.db`` SQLite database that
:class:`devclaw.state_store.StateStore` already owns (WAL, one shared
connection guarded by a single ``threading.RLock``), so a task row and its
owning goal's state can be written in one atomic :meth:`StateStore.transaction`.

**PR3 brought ``goal_status`` + ``goal_phase_history`` LIVE.** ``GoalState``
now owns the status read/write surface (:meth:`read_status` /
:meth:`write_status` / the phase-history methods); ``GoalStore.load_status`` /
``save_status`` are re-backed onto it, with ``STATUS.md`` demoted to a
generated full-fidelity view (the rollback path). The remaining tables
(``goal_steering`` / ``goal_log`` / ``goal_deliveries`` / ``goal_settlements``
/ ``goal_docs``) stay created-but-empty until later PRs migrate onto them.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from .models import GoalStatus, InFlight

if TYPE_CHECKING:
    from ..state_store import StateStore


def _now_ms() -> int:
    return int(time.time() * 1000)


class GoalState:
    """Owns the goal-state tables inside a shared :class:`StateStore`.

    Handed a ``StateStore``, it borrows that store's single sqlite connection,
    its ``RLock``, and its ``transaction()`` seam — so goal-state writes land in
    the same database and can join the same atomic unit as task/program writes.
    Construction bootstraps the tables idempotently (``CREATE TABLE IF NOT
    EXISTS`` + forward-compat ALTERs), mirroring the store's own ``_bootstrap``
    style. Since PR3 it also carries the status read/write surface
    (``goal_status`` + ``goal_phase_history``); the other tables stay unused
    until later Tranche 1 PRs migrate onto them.
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
                -- One row per goal: the machine state STATUS.md held in YAML
                -- frontmatter before Tranche 1/PR3. This table is now the
                -- source of truth for status; STATUS.md is a generated
                -- full-fidelity view written on every save (the rollback path).
                -- `phase`/`lifecycle` are the current GoalStatus fields;
                -- `state` (PR4) holds the consolidated devclaw.goal.transitions
                -- .State value, stamped by GoalStore on every write (nullable
                -- only for a pre-PR4 row that hasn't been re-saved yet).
                -- `version` (PR4) is the optimistic-concurrency counter
                -- GoalStore.transition() CAS's against, bumped by exactly 1 on
                -- every write; in_flight_* carry the durable pointer to the
                -- goal's running task/program (in_flight_json is the
                -- authoritative serialized InFlight, ref_id/kind denormalized
                -- for later indexing).
                CREATE TABLE IF NOT EXISTS goal_status (
                  goal_id               TEXT PRIMARY KEY,
                  version               INTEGER NOT NULL DEFAULT 0,
                  state                 TEXT,
                  phase                 TEXT,
                  lifecycle             TEXT,
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

            # Forward-compat ALTERs for DBs bootstrapped by PR2 (which created
            # goal_status WITHOUT phase/lifecycle — those columns were only added
            # in PR3, when the table went live). Idempotent: a fresh DB already
            # has them from the CREATE above, so the duplicate-column error is
            # swallowed. Mirrors StateStore._bootstrap's ALTER pattern.
            for sql in (
                "ALTER TABLE goal_status ADD COLUMN phase TEXT",
                "ALTER TABLE goal_status ADD COLUMN lifecycle TEXT",
            ):
                try:
                    self._store._db.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            self._store._commit()

    # ---- goal_status persistence (PR3: STATUS.md re-backed onto SQLite) ----
    #
    # The store orchestrates STATUS.md-as-view + lazy migration; these methods
    # are the pure DB surface. All borrow the shared connection + lock and use
    # the store's _commit() (a no-op inside an open transaction()), so a status
    # write can join the same atomic unit as a task write in a later PR.

    def has_status(self, goal_id: str) -> bool:
        """Whether a ``goal_status`` row exists — the lazy-migration guard."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_status WHERE goal_id = ? LIMIT 1", (goal_id,)
            ).fetchone()
        return row is not None

    def current_phase(self, goal_id: str) -> "str | None":
        """The stored phase, or None when no row exists. Read by save_status to
        decide whether the phase changed (and a phase_history entry is due)."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT phase FROM goal_status WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        return row["phase"] if row else None

    def read_status(self, goal_id: str) -> GoalStatus:
        """Rehydrate the full :class:`GoalStatus` (incl. in_flight + phase
        history). Caller must ensure a row exists (see :meth:`has_status`)."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT * FROM goal_status WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        return _row_to_status(row, self.read_phase_history(goal_id))

    def write_status(self, goal_id: str, status: GoalStatus) -> None:
        """Upsert the status row. The InFlight is serialized to in_flight_json
        (authoritative) with id/kind denormalized for later indexing; the
        phase_history tuple is NOT written here — it lives in
        goal_phase_history (see :meth:`append_phase_history`).

        ``version`` is bumped by exactly 1 on EVERY write — 1 on the first
        INSERT, ``version + 1`` on every subsequent UPDATE — the counter
        GoalStore.transition() CAS's against and computes its return value
        from (``fresh.version + 1``) without a re-read. ``state`` is written
        verbatim from ``status.state``: this method is a pure DB write, not a
        projector — the CALLER (GoalStore.save_status / .transition /
        .force_block) is responsible for stamping the derived
        devclaw.goal.transitions.State value onto ``status`` first."""
        in_flight_json = None
        in_flight_ref_id = None
        in_flight_kind = None
        if status.in_flight is not None:
            f = status.in_flight
            in_flight_ref_id = f.id
            in_flight_kind = f.ref_kind
            in_flight_json = json.dumps(
                {
                    "engine": f.engine,
                    "tool": f.tool,
                    "id": f.id,
                    "ref_kind": f.ref_kind,
                    "goal": f.goal,
                    "is_done_check": f.is_done_check,
                    "is_discovery": f.is_discovery,
                    "addresses": list(f.addresses),
                }
            )
        with self._store._lock:
            self._store._db.execute(
                """
                INSERT INTO goal_status (
                  goal_id, version, state, phase, lifecycle, blocked_on, "next",
                  last_plan_at, last_tick_at, actions_dispatched, deliveries_since_eval,
                  last_eval_verdict, last_eval_at, last_eval_note, last_progress_at,
                  no_progress_notified, in_flight_ref_id, in_flight_kind,
                  in_flight_json, inbox_ingest_cursor, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                  version               = goal_status.version + 1,
                  state                 = excluded.state,
                  phase                 = excluded.phase,
                  lifecycle             = excluded.lifecycle,
                  blocked_on            = excluded.blocked_on,
                  "next"                = excluded."next",
                  last_plan_at          = excluded.last_plan_at,
                  last_tick_at          = excluded.last_tick_at,
                  actions_dispatched    = excluded.actions_dispatched,
                  deliveries_since_eval = excluded.deliveries_since_eval,
                  last_eval_verdict     = excluded.last_eval_verdict,
                  last_eval_at          = excluded.last_eval_at,
                  last_eval_note        = excluded.last_eval_note,
                  last_progress_at      = excluded.last_progress_at,
                  no_progress_notified  = excluded.no_progress_notified,
                  in_flight_ref_id      = excluded.in_flight_ref_id,
                  in_flight_kind        = excluded.in_flight_kind,
                  in_flight_json        = excluded.in_flight_json,
                  inbox_ingest_cursor   = excluded.inbox_ingest_cursor,
                  updated_at            = excluded.updated_at
                """,
                (
                    goal_id,
                    status.state,
                    status.phase,
                    status.lifecycle,
                    status.blocked_on,
                    status.next,
                    status.last_plan_at,
                    status.last_tick_at,
                    status.actions_dispatched,
                    status.deliveries_since_eval,
                    status.last_eval_verdict,
                    status.last_eval_at,
                    status.last_eval_note,
                    status.last_progress_at,
                    1 if status.no_progress_notified else 0,
                    in_flight_ref_id,
                    in_flight_kind,
                    in_flight_json,
                    status.inbox_cursor,
                    _now_ms(),
                ),
            )
            self._store._commit()

    #: telemetry-only GoalStatus fields GoalStore.update_status_fields() may
    #: touch via :meth:`update_columns` — a column-only UPDATE, never a
    #: full-row rewrite (the mechanism that keeps a stale-snapshot bookkeeping
    #: write from ever clobbering a concurrent phase/lifecycle/in_flight
    #: transition). Keys are GoalStatus field names; values are the
    #: `goal_status` column name (identical today, kept as a mapping so a
    #: future rename only touches one side).
    STATUS_FIELD_COLUMNS: "dict[str, str]" = {
        "last_plan_at": "last_plan_at",
        "last_tick_at": "last_tick_at",
        "last_progress_at": "last_progress_at",
        "no_progress_notified": "no_progress_notified",
        "last_eval_verdict": "last_eval_verdict",
        "last_eval_at": "last_eval_at",
        "last_eval_note": "last_eval_note",
        "deliveries_since_eval": "deliveries_since_eval",
    }

    def update_columns(self, goal_id: str, fields: dict) -> None:
        """Column-only ``UPDATE`` for telemetry fields — the mechanism behind
        :meth:`GoalStore.update_status_fields`. Bumps ``version`` by 1 like
        every other write, but touches ONLY the named columns (never phase/
        lifecycle/in_flight/blocked_on/next), so it can never be the write
        that clobbers a concurrent state transition. Caller has already
        validated ``fields`` keys against :data:`STATUS_FIELD_COLUMNS`; a
        no-op (no SQL issued) on an empty dict."""
        if not fields:
            return
        sets = []
        params: list = []
        for key, value in fields.items():
            col = self.STATUS_FIELD_COLUMNS[key]
            if key == "no_progress_notified":
                value = 1 if value else 0
            sets.append(f"{col} = ?")
            params.append(value)
        params.append(_now_ms())
        params.append(goal_id)
        with self._store._lock:
            self._store._db.execute(
                f"UPDATE goal_status SET {', '.join(sets)}, version = version + 1, "
                "updated_at = ? WHERE goal_id = ?",
                params,
            )
            self._store._commit()

    # ---- goal_phase_history (append-only phase transitions) ----------------

    def read_phase_history(self, goal_id: str) -> "tuple[dict, ...]":
        """The goal's phase transitions in append order — the tuple that lands
        on ``GoalStatus.phase_history`` and in the STATUS.md view."""
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT phase, at FROM goal_phase_history WHERE goal_id = ? ORDER BY id ASC",
                (goal_id,),
            ).fetchall()
        return tuple({"phase": r["phase"], "at": r["at"]} for r in rows)

    def append_phase_history(self, goal_id: str, phase: str, at: str) -> None:
        """Append one phase transition. Called by save_status when the phase
        differs from what's stored (one entry per entry-to-a-new-phase)."""
        with self._store._lock:
            self._store._db.execute(
                "INSERT INTO goal_phase_history (goal_id, phase, at) VALUES (?, ?, ?)",
                (goal_id, phase, at),
            )
            self._store._commit()

    def seed_phase_history(self, goal_id: str, entries: "tuple[dict, ...]") -> None:
        """Bulk-insert existing phase entries verbatim — the lazy migration path
        that carries a legacy STATUS.md's phase_history onto the table."""
        if not entries:
            return
        with self._store._lock:
            self._store._db.executemany(
                "INSERT INTO goal_phase_history (goal_id, phase, at) VALUES (?, ?, ?)",
                [(goal_id, str(e["phase"]), str(e["at"])) for e in entries],
            )
            self._store._commit()


def _row_to_status(row, phase_history: "tuple[dict, ...]") -> GoalStatus:
    """Reconstruct a :class:`GoalStatus` from a ``goal_status`` row + its phase
    history. Mirrors the field-by-field degrade of the old STATUS.md reader so a
    migrated goal loads identically."""
    in_flight = None
    if row["in_flight_json"]:
        f = json.loads(row["in_flight_json"])
        raw_addr = f.get("addresses") or []
        addresses = (
            [str(a) for a in raw_addr if str(a).strip()]
            if isinstance(raw_addr, list)
            else []
        )
        in_flight = InFlight(
            engine=f["engine"],
            tool=f["tool"],
            id=f["id"],
            ref_kind=f["ref_kind"],
            goal=f.get("goal", ""),
            is_done_check=bool(f.get("is_done_check", False)),
            is_discovery=bool(f.get("is_discovery", False)),
            addresses=addresses,
        )
    return GoalStatus(
        phase=row["phase"] or "idle",
        lifecycle=row["lifecycle"] or None,
        in_flight=in_flight,
        blocked_on=row["blocked_on"] or None,
        next=row["next"] or "",
        last_plan_at=row["last_plan_at"] or None,
        last_tick_at=row["last_tick_at"] or None,
        inbox_cursor=int(row["inbox_ingest_cursor"] or 0),
        actions_dispatched=int(row["actions_dispatched"] or 0),
        deliveries_since_eval=int(row["deliveries_since_eval"] or 0),
        last_eval_verdict=row["last_eval_verdict"] or None,
        last_eval_at=row["last_eval_at"] or None,
        last_eval_note=row["last_eval_note"] or "",
        last_progress_at=row["last_progress_at"] or None,
        no_progress_notified=bool(row["no_progress_notified"]),
        phase_history=phase_history,
        state=row["state"] or None,
        version=int(row["version"] or 0),
    )
