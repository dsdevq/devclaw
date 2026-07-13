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
generated full-fidelity view (the rollback path).

**PR5 brought ``goal_steering`` LIVE.** ``consumed_at IS NULL`` is now the
source of truth for "unread" — ``GoalStore.append_steering`` /
``_ingest_inbox`` write rows, ``GoalStore.transition(consume_steering=...)``
consumes them by exact id. ``inbox.md`` stays both the human-readable mirror
and a hand-append input (lazily ingested into rows).

**PR6 brought ``goal_log`` / ``goal_deliveries`` / ``goal_docs`` LIVE.**
``goal_log`` and ``goal_deliveries`` are row-backed with ``log.md`` /
``deliveries.md`` as generated mirrors (lazily migrated, same shape as
PR3/PR5); ``goal_deliveries`` also gained idempotent inserts keyed on a
nullable ``ref_id`` (``UNIQUE(goal_id, ref_id)`` + INSERT OR IGNORE), closing
a PR4-review nuance where a settle landing in a ``TransitionConflict`` retry
window could append the same delivery twice. ``goal_docs`` now backs
``checklist.yaml`` / ``firmed-draft.yaml`` (kinds ``checklist`` /
``firmed_draft``) — the torn-write class T0.4 hardened the file view against
(``tmp`` + ``os.replace``) becomes structurally impossible once a goal has a
row, courtesy of SQLite's atomic upsert. ``goal_settlements`` stays
created-but-empty until a later PR migrates onto it.
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
                  blocked_kind          TEXT,
                  heal_attempts         INTEGER NOT NULL DEFAULT 0,
                  next_heal_at          TEXT,
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

                -- Steering lines (inbox.md is the human-readable mirror +
                -- hand-append input — PR5). consumed_at NULL == unread, the
                -- source of truth for what the planner hasn't seen yet;
                -- GoalStore.transition(consume_steering=[...]) stamps it,
                -- atomically with the decision the steering informed.
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
                -- today, PR6). ref_id is NULLABLE: NULL for legacy-ingested
                -- sections and any plain append (unconditional INSERT); a
                -- settle's dispatched-ref id makes the INSERT idempotent —
                -- SQLite treats every NULL as distinct under UNIQUE, so only
                -- non-NULL ref_ids actually dedupe via INSERT OR IGNORE (see
                -- GoalState.append_delivery_row).
                CREATE TABLE IF NOT EXISTS goal_deliveries (
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id     TEXT NOT NULL,
                  ref_id      TEXT,
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

                -- Free-form per-goal documents keyed by kind. PR6 lands two
                -- kinds LIVE — "checklist" (checklist.yaml) and
                -- "firmed_draft" (firmed-draft.yaml), the acceptance-contract
                -- artifacts T0.4 hardened against torn writes; SQLite's atomic
                -- upsert makes that write-tear class structurally impossible
                -- once a goal has a row. spec.md / discovery.md stay plain
                -- files (display/prompt inputs, not consumed-state) — a later
                -- PR if ever. PRIMARY KEY(goal_id, kind) == one current doc
                -- per kind.
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
            # in PR3, when the table went live) and for pre-blocked_kind DBs
            # (the column landed with the F8-prerequisite block classification).
            # Idempotent: a fresh DB already has them from the CREATE above, so
            # the duplicate-column error is swallowed. Mirrors
            # StateStore._bootstrap's ALTER pattern.
            for sql in (
                "ALTER TABLE goal_status ADD COLUMN phase TEXT",
                "ALTER TABLE goal_status ADD COLUMN lifecycle TEXT",
                "ALTER TABLE goal_status ADD COLUMN blocked_kind TEXT",
                "ALTER TABLE goal_status ADD COLUMN heal_attempts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE goal_status ADD COLUMN next_heal_at TEXT",
            ):
                try:
                    self._store._db.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            self._migrate_deliveries_ref_id_nullable()
            self._store._commit()

    def _migrate_deliveries_ref_id_nullable(self) -> None:
        """Forward-compat schema fix for DBs bootstrapped by PR2: this table
        was created with ``ref_id TEXT NOT NULL`` before anything ever wrote
        to it (``goal_deliveries`` stayed empty until PR6). PR6's idempotent
        ``append_delivery_row`` needs ``ref_id`` NULLABLE — legacy-ingested
        deliveries.md sections and any plain (non-idempotent) append have no
        ref_id to key on. SQLite has no ``ALTER COLUMN`` to drop a NOT NULL
        constraint in place, so this recreates the table with the corrected
        schema (the standard SQLite copy/drop/rename dance) — safe because
        the table has been unused, but written to preserve any existing rows
        regardless. Guarded on ``PRAGMA table_info`` so it only ever runs
        once per DB: a fresh table (created nullable by the CREATE TABLE IF
        NOT EXISTS above) already has ``notnull=0`` and this is a no-op."""
        with self._store._lock:
            info = self._store._db.execute("PRAGMA table_info(goal_deliveries)").fetchall()
            ref_id_col = next((r for r in info if r["name"] == "ref_id"), None)
            if ref_id_col is None or not ref_id_col["notnull"]:
                return
            self._store._db.executescript(
                """
                CREATE TABLE goal_deliveries__pr6_nullable_ref_id (
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id     TEXT NOT NULL,
                  ref_id      TEXT,
                  instruction TEXT,
                  body        TEXT,
                  created_at  INTEGER NOT NULL,
                  UNIQUE(goal_id, ref_id)
                );
                INSERT INTO goal_deliveries__pr6_nullable_ref_id
                  (id, goal_id, ref_id, instruction, body, created_at)
                  SELECT id, goal_id, ref_id, instruction, body, created_at
                  FROM goal_deliveries;
                DROP TABLE goal_deliveries;
                ALTER TABLE goal_deliveries__pr6_nullable_ref_id RENAME TO goal_deliveries;
                CREATE INDEX IF NOT EXISTS idx_goal_deliveries_goal
                  ON goal_deliveries(goal_id, id);
                """
            )

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
                  goal_id, version, state, phase, lifecycle, blocked_on, blocked_kind,
                  heal_attempts, next_heal_at, "next",
                  last_plan_at, last_tick_at, actions_dispatched, deliveries_since_eval,
                  last_eval_verdict, last_eval_at, last_eval_note, last_progress_at,
                  no_progress_notified, in_flight_ref_id, in_flight_kind,
                  in_flight_json, inbox_ingest_cursor, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                  version               = goal_status.version + 1,
                  state                 = excluded.state,
                  phase                 = excluded.phase,
                  lifecycle             = excluded.lifecycle,
                  blocked_on            = excluded.blocked_on,
                  blocked_kind          = excluded.blocked_kind,
                  heal_attempts         = excluded.heal_attempts,
                  next_heal_at          = excluded.next_heal_at,
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
                    status.blocked_kind,
                    status.heal_attempts,
                    status.next_heal_at,
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
        # heal_attempts / next_heal_at are damping bookkeeping (never read by
        # derive_state) — the column-only path exists so the auto-heal's
        # gave-up marker and the prep-recheck backoff window can be stamped
        # on a still-BLOCKED goal without a full-row rewrite.
        "heal_attempts": "heal_attempts",
        "next_heal_at": "next_heal_at",
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

    def set_inbox_ingest_cursor(self, goal_id: str, n: int) -> None:
        """Column-only ``UPDATE`` of ``inbox_ingest_cursor`` — the PR5 write
        side of the ingest boundary (how many ``inbox.md`` lines have been
        converted into ``goal_steering`` rows, NOT how many are consumed —
        see :meth:`GoalStore._ingest_inbox`). Bumps ``version`` by 1 like
        every other write (the PR4 rule: every write bumps version), which is
        WHY callers that hold an in-flight ``status`` snapshot spanning an
        ingest must reload before using it as a later ``transition()``
        ``expect=`` — a stale version there would self-conflict against this
        write, not a real race. Caller ensures the row exists first (mirrors
        :meth:`update_columns`)."""
        with self._store._lock:
            self._store._db.execute(
                "UPDATE goal_status SET inbox_ingest_cursor = ?, version = version + 1, "
                "updated_at = ? WHERE goal_id = ?",
                (n, _now_ms(), goal_id),
            )
            self._store._commit()

    # ---- goal_steering (steering rows — PR5 consumed-at source of truth) ---
    #
    # ``consumed_at IS NULL`` == unread. Rows are the source of truth for
    # WHAT is unread; ``goal_status.inbox_ingest_cursor`` (above) is a
    # SEPARATE, unrelated boundary — how far into ``inbox.md`` the rows
    # extend, so a hand-typed line is only ever ingested once. Consumption
    # (stamping ``consumed_at``) happens ONLY via :meth:`consume_steering_rows`,
    # called from :meth:`GoalStore.transition` so it rides the SAME CAS'd
    # transaction as the decision the steering informed.

    def has_steering_rows(self, goal_id: str) -> bool:
        """Whether ANY ``goal_steering`` row (consumed or not) exists yet —
        the lazy-migration guard in :meth:`GoalStore._ingest_inbox`: the
        pre-PR5 history backfill may only run ONCE, on the very first ingest
        for a goal that predates row-backed steering. Idempotent by
        construction — once any row exists, this returns True forever."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_steering WHERE goal_id = ? LIMIT 1", (goal_id,)
            ).fetchone()
        return row is not None

    def append_steering_rows(
        self, goal_id: str, lines: "list[str]", *, source: str,
        created_at_ms: "int | None" = None, consumed: bool = False,
    ) -> "list[int]":
        """INSERT one ``goal_steering`` row per line, in order. ``line`` is
        stored VERBATIM — callers that ingest hand-typed ``inbox.md`` content
        pass the raw line (which may itself carry an old ``[source ts]``
        prefix; this method never parses it). ``consumed=True`` stamps
        ``consumed_at = created_at`` immediately — used ONLY by the lazy
        pre-PR5 migration to mark already-acted-on history so it's never
        re-fed to the planner; the steering-append default (``consumed=False``)
        leaves ``consumed_at`` NULL, per the new unread-by-row-id model.
        Returns the inserted rowids in insertion (== id) order."""
        if not lines:
            return []
        ts = created_at_ms if created_at_ms is not None else _now_ms()
        ids: list[int] = []
        with self._store._lock:
            for line in lines:
                cur = self._store._db.execute(
                    "INSERT INTO goal_steering (goal_id, source, line, created_at, consumed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (goal_id, source, line, ts, ts if consumed else None),
                )
                ids.append(cur.lastrowid)
            self._store._commit()
        return ids

    def unread_steering_rows(self, goal_id: str) -> "list[sqlite3.Row]":
        """Unconsumed ``goal_steering`` rows, oldest first — ``consumed_at IS
        NULL`` is the unread marker PR5 makes the source of truth. Each row
        carries ``id`` / ``source`` / ``line`` (plus the sqlite defaults);
        callers needing exact-id consumption read ``row["id"]`` and thread it
        into :meth:`GoalStore.transition`'s ``consume_steering=``."""
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT id, source, line FROM goal_steering "
                "WHERE goal_id = ? AND consumed_at IS NULL ORDER BY id ASC",
                (goal_id,),
            ).fetchall()
        return rows

    def consume_steering_rows(self, goal_id: str, ids: "list[int]", consumed_at_ms: int) -> None:
        """Stamp ``consumed_at`` on EXACTLY the given row ids — the exact-id
        consumption :meth:`GoalStore.transition`'s ``consume_steering=``
        threads through, so a row inserted mid-plan (not among ``ids``) keeps
        ``consumed_at`` NULL and is seen next tick — the fix for
        "steer-during-planner-await lost" (the old count-based cursor
        consumed EVERYTHING that existed at write time, including rows the
        planner never saw). No-op on empty ``ids`` (also avoids an ``IN ()``
        empty-tuple SQL error). The ``AND consumed_at IS NULL`` guard makes a
        double-consume of the same id a no-op rather than clobbering an
        earlier (real) consumed_at timestamp."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._store._lock:
            self._store._db.execute(
                f"UPDATE goal_steering SET consumed_at = ? "
                f"WHERE goal_id = ? AND id IN ({placeholders}) AND consumed_at IS NULL",
                (consumed_at_ms, goal_id, *ids),
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

    # ---- goal_log (append-only event log — log.md today, PR6) --------------
    #
    # log.md is a pure OUTPUT view — unlike inbox.md, nothing hand-appends to
    # it — so migration is a true one-shot with no ongoing ingest cursor: once
    # ANY row exists for a goal, :meth:`GoalStore._ingest_log` never runs
    # again for it. Rows store the MIRROR-FORMATTED line verbatim (the PR5
    # rule — see ``append_steering``), so ``recent_log`` reads back
    # byte-identical text to the pre-PR6 file-tail read.

    def has_log_rows(self, goal_id: str) -> bool:
        """Whether ANY ``goal_log`` row exists yet — the lazy-migration guard
        :meth:`GoalStore._ingest_log` uses so a legacy log.md is ingested
        exactly once."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_log WHERE goal_id = ? LIMIT 1", (goal_id,)
            ).fetchone()
        return row is not None

    def append_log_row(self, goal_id: str, line: str, ts_ms: int) -> None:
        """INSERT one ``goal_log`` row. ``line`` is the FULL formatted mirror
        line (``- [<iso>] <message>``), stored verbatim in ``message`` — the
        same mirror-formatted-text rule PR5's steering rows follow. ``ts`` is
        ordering-only (the ms clock; nothing parses it back out for display)."""
        with self._store._lock:
            self._store._db.execute(
                "INSERT INTO goal_log (goal_id, ts, message) VALUES (?, ?, ?)",
                (goal_id, ts_ms, line),
            )
            self._store._commit()

    def append_log_rows(self, goal_id: str, lines: "list[str]", ts_ms: int) -> None:
        """Bulk INSERT, in order — the one-shot lazy-migration path that
        carries a legacy log.md's lines onto rows verbatim, in file order.
        No-op on an empty list (skips a pointless commit)."""
        if not lines:
            return
        with self._store._lock:
            self._store._db.executemany(
                "INSERT INTO goal_log (goal_id, ts, message) VALUES (?, ?, ?)",
                [(goal_id, ts_ms, line) for line in lines],
            )
            self._store._commit()

    def recent_log_rows(self, goal_id: str, n: int) -> "list[str]":
        """The last ``n`` ``message`` values, in natural (ascending) order —
        mirrors the pre-PR6 file read's ``lines[-n:]`` slice. Queried
        ``ORDER BY id DESC LIMIT n`` (cheap on the goal_id+id index) then
        reversed in Python, since SQL has no "last n in original order" in
        one direction."""
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT message FROM goal_log WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
                (goal_id, n),
            ).fetchall()
        return [r["message"] for r in reversed(rows)]

    def all_log_rows(self, goal_id: str) -> "list[str]":
        """Every ``message`` for ``goal_id``, in natural (ascending) order —
        unlike :meth:`recent_log_rows` (bounded tail), this reads the FULL
        history. Used by :meth:`GoalStore._seed_settlements`'s one-shot scan,
        which needs to see every historical settle line, not just the recent
        tail, to seed ``goal_settlements`` identically to what the old
        ``log_contains(f" {id} → ")`` guard used to answer."""
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT message FROM goal_log WHERE goal_id = ? ORDER BY id ASC",
                (goal_id,),
            ).fetchall()
        return [r["message"] for r in rows]

    # ---- goal_deliveries (grounded evidence — deliveries.md today, PR6) ----
    #
    # Same mirror-formatted-text rule as goal_log. ``ref_id`` is the
    # idempotency key a settle passes (the in-flight ref's id): NULL means
    # "no dedup key" (legacy-ingested sections, or any caller not passing
    # one) and is always inserted; non-NULL goes through ``INSERT OR IGNORE``
    # against ``UNIQUE(goal_id, ref_id)`` — closing the PR4-review nuance
    # where a ``TransitionConflict`` landing in the settle-retry window could
    # append the SAME delivery twice.

    def has_delivery_rows(self, goal_id: str) -> bool:
        """Whether ANY ``goal_deliveries`` row exists yet — the lazy-migration
        guard :meth:`GoalStore._ingest_deliveries` uses."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_deliveries WHERE goal_id = ? LIMIT 1", (goal_id,)
            ).fetchone()
        return row is not None

    def append_delivery_row(
        self, goal_id: str, ref_id: "str | None", block: str, ts_ms: int,
        *, instruction: str = "",
    ) -> bool:
        """INSERT one ``goal_deliveries`` row. ``block`` is the FULL rendered
        mirror section (``## [<iso>] <instruction>\\n\\n<body>\\n\\n``), stored
        verbatim in ``body`` so :meth:`GoalStore.recent_deliveries`'s
        reconstruction is byte-identical to the pre-PR6 file-tail read.
        ``instruction`` is denormalized into its own column for future
        queries (never parsed back out of ``block``).

        ``ref_id=None`` → a plain, unconditional INSERT (no idempotency key).
        ``ref_id`` set → ``INSERT OR IGNORE``: a duplicate ref_id for this
        goal is silently dropped (the retry-window fix — see the section
        docstring above). Returns True iff a row was actually inserted, so
        the caller (``GoalStore.append_delivery``) can skip the file mirror
        too on the ignored path — a duplicate ref_id must never produce a
        duplicate section in deliveries.md."""
        with self._store._lock:
            if ref_id is None:
                cur = self._store._db.execute(
                    "INSERT INTO goal_deliveries (goal_id, ref_id, instruction, body, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (goal_id, instruction, block, ts_ms),
                )
            else:
                cur = self._store._db.execute(
                    "INSERT OR IGNORE INTO goal_deliveries "
                    "(goal_id, ref_id, instruction, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (goal_id, ref_id, instruction, block, ts_ms),
                )
            inserted = cur.rowcount == 1
            self._store._commit()
        return inserted

    def recent_delivery_blocks(self, goal_id: str) -> "list[str]":
        """Every delivery ``body`` for ``goal_id``, oldest first.
        :meth:`GoalStore.recent_deliveries` char-tails the joined text, so it
        needs the FULL sequence — goals carry at most a few dozen deliveries,
        so reading them all is fine."""
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT body FROM goal_deliveries WHERE goal_id = ? ORDER BY id ASC",
                (goal_id,),
            ).fetchall()
        return [r["body"] for r in rows]

    # ---- goal_docs (the acceptance contract — checklist/firmed-draft, PR6) -
    #
    # One current document per (goal_id, kind), upserted atomically — the
    # torn-write class T0.4 hardened the FILE view against (tmp + os.replace)
    # is structurally impossible here: a crash mid-write leaves either the OLD
    # row or nothing touched, never a half-written one.

    #: The kinds backed with rows. ``checklist``/``firmed_draft`` (PR6) are the
    #: acceptance contract; ``repo_analysis`` (triage F2) is the raw
    #: review_repository output persisted at discovery settle — row-only, no
    #: file view (up to ~200KB of machine-read ground truth for the
    #: decomposer, not a human-skimmable artifact). spec/discovery stay plain
    #: files (display/prompt inputs, not consumed-state).
    DOC_KINDS = frozenset({"checklist", "firmed_draft", "repo_analysis"})

    def has_doc(self, goal_id: str, kind: str) -> bool:
        """Whether a ``goal_docs`` row exists for ``(goal_id, kind)`` — the
        DB-row-vs-legacy-file branch :meth:`GoalStore.read_checklist` /
        :meth:`GoalStore.read_firmed_draft` use."""
        assert kind in self.DOC_KINDS, f"has_doc: unknown kind {kind!r}"
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_docs WHERE goal_id = ? AND kind = ? LIMIT 1",
                (goal_id, kind),
            ).fetchone()
        return row is not None

    def write_doc(self, goal_id: str, kind: str, content: str, ts_ms: int) -> None:
        """Upsert the current document for ``(goal_id, kind)``."""
        assert kind in self.DOC_KINDS, f"write_doc: unknown kind {kind!r}"
        with self._store._lock:
            self._store._db.execute(
                "INSERT INTO goal_docs (goal_id, kind, content, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(goal_id, kind) DO UPDATE SET "
                "content = excluded.content, updated_at = excluded.updated_at",
                (goal_id, kind, content, ts_ms),
            )
            self._store._commit()

    def read_doc(self, goal_id: str, kind: str) -> "str | None":
        """The current document content, or None if no row exists yet
        (legacy goal pre-migration, or a goal this doc's phase hasn't
        reached — e.g. no decomposer run, no firming run)."""
        assert kind in self.DOC_KINDS, f"read_doc: unknown kind {kind!r}"
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT content FROM goal_docs WHERE goal_id = ? AND kind = ?",
                (goal_id, kind),
            ).fetchone()
        return row["content"] if row else None

    # ---- goal_settlements (settled-and-recorded truth — PR7) --------------
    #
    # One row per settled in-flight ref. Table exists since PR2 (created
    # empty); PR7 is the first thing that reads/writes it. No file mirror —
    # there's no settlements.md view, so these are plain row writes that
    # simply join whichever transaction() (if any) is open, same as every
    # other GoalState write.

    def has_settlement(self, goal_id: str, ref_id: str) -> bool:
        """Whether ``ref_id`` has a recorded settlement for ``goal_id`` — the
        row-backed replacement for the old ``log_contains(f" {id} → ")``
        string-match guard."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_settlements WHERE goal_id = ? AND ref_id = ? LIMIT 1",
                (goal_id, ref_id),
            ).fetchone()
        return row is not None

    def has_any_settlements(self, goal_id: str) -> bool:
        """Whether ANY settlement row exists yet for ``goal_id`` — the
        lazy-seed guard :meth:`GoalStore._seed_settlements` uses so the
        historical-log scan runs at most once per goal."""
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM goal_settlements WHERE goal_id = ? LIMIT 1", (goal_id,)
            ).fetchone()
        return row is not None

    def record_settlement(
        self, goal_id: str, ref_id: str, ref_kind: "str | None", status: "str | None",
        settled_at_ms: int,
    ) -> bool:
        """INSERT OR IGNORE one settlement row. Idempotent against
        ``UNIQUE(goal_id, ref_id)`` — a settle txn retried after a
        TransitionConflict rollback (or the lazy-seed re-scanning a line
        whose token collides with a real settlement already recorded) is a
        silent no-op, same dedup shape as :meth:`append_delivery_row`.
        Returns True iff a row was actually inserted."""
        with self._store._lock:
            cur = self._store._db.execute(
                "INSERT OR IGNORE INTO goal_settlements "
                "(goal_id, ref_id, ref_kind, status, settled_at) VALUES (?, ?, ?, ?, ?)",
                (goal_id, ref_id, ref_kind, status, settled_at_ms),
            )
            inserted = cur.rowcount == 1
            self._store._commit()
        return inserted


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
        # NULL on a row that predates the column (pre-blocked_kind DB, lazily
        # ALTERed by _bootstrap) reads as "" — unclassified, same as the default.
        blocked_kind=row["blocked_kind"] or "",
        heal_attempts=int(row["heal_attempts"] or 0),
        next_heal_at=row["next_heal_at"] or None,
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
