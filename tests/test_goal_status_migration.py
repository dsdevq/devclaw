"""Tranche 1 / PR3 — GoalStore.load_status / save_status re-backed onto the
``goal_status`` SQLite table, with STATUS.md demoted to a generated
full-fidelity view.

Pins the four load-bearing properties:
  * round-trip fidelity (every GoalStatus field, incl. in_flight + phase_history)
  * STATUS.md stays a faithful view — the rollback path (revert PR3 → the
    current frontmatter reader recovers the exact saved state)
  * lazy migration of a legacy STATUS.md is correct AND idempotent
  * phase_history accumulates in the table across saves (the merge hack is gone)
"""

from __future__ import annotations

from devclaw.goal.models import GoalStatus, InFlight
from devclaw.goal.store import GoalStore
from tests.goal_fakes import Clock


def _rich_status() -> GoalStatus:
    """A GoalStatus that exercises every column + a populated in_flight."""
    return GoalStatus(
        phase="verifying",
        lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "review_repository", "t42", "task", "verify done",
            is_done_check=True, is_discovery=False, addresses=["i1", "i2"],
        ),
        blocked_on=None,
        next="verify the done gate",
        last_plan_at="2026-06-06T12:00:00+00:00",
        last_tick_at="2026-06-06T12:05:00+00:00",
        inbox_cursor=4,
        actions_dispatched=7,
        deliveries_since_eval=2,
        last_eval_verdict="on_track",
        last_eval_at="2026-06-06T12:04:00+00:00",
        last_eval_note="progressing nicely",
        last_progress_at="2026-06-06T12:03:00+00:00",
        no_progress_notified=True,
    )


# ---- round-trip fidelity ---------------------------------------------------


def test_save_then_load_roundtrips_every_field(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", _rich_status())
    back = store.load_status("g")

    assert back.phase == "verifying"
    assert back.lifecycle == "executing"
    assert back.blocked_on is None
    assert back.next == "verify the done gate"
    assert back.last_plan_at == "2026-06-06T12:00:00+00:00"
    assert back.last_tick_at == "2026-06-06T12:05:00+00:00"
    assert back.inbox_cursor == 4
    assert back.actions_dispatched == 7
    assert back.deliveries_since_eval == 2
    assert back.last_eval_verdict == "on_track"
    assert back.last_eval_at == "2026-06-06T12:04:00+00:00"
    assert back.last_eval_note == "progressing nicely"
    assert back.last_progress_at == "2026-06-06T12:03:00+00:00"
    assert back.no_progress_notified is True
    # in_flight rehydrates fully (all flags + addresses)
    assert back.in_flight == InFlight(
        "devclaw", "review_repository", "t42", "task", "verify done",
        is_done_check=True, is_discovery=False, addresses=["i1", "i2"],
    )
    # phase_history got its one transition entry (idle→verifying)
    assert [e["phase"] for e in back.phase_history] == ["verifying"]
    # a second load is stable — GoalStatus is a frozen dataclass with value eq
    assert store.load_status("g") == back


def test_none_in_flight_roundtrips(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="investigating"))
    back = store.load_status("g")
    assert back.in_flight is None
    assert back.phase == "idle"
    assert back.lifecycle == "investigating"


# ---- STATUS.md is a faithful view — the rollback property ------------------


def test_status_md_view_recovers_state_via_current_reader(tmp_path):
    """After save_status, the on-disk STATUS.md alone — parsed by the CURRENT
    frontmatter reader — recovers the exact saved state. This is the rollback
    guarantee: revert PR3 and load_status reads STATUS.md again, no data lost."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", _rich_status())

    text = (tmp_path / "g" / "STATUS.md").read_text()
    # `_parse_status_md` IS the pre-PR3 STATUS.md reader; the DB is the truth.
    recovered_from_file = GoalStore._parse_status_md(text)
    truth_from_db = store.load_status("g")
    assert recovered_from_file == truth_from_db

    # The frontmatter shape the reader depends on is intact + reflects the save.
    fm = GoalStore._read_frontmatter(text)
    assert fm["phase"] == "verifying"
    assert fm["lifecycle"] == "executing"
    assert fm["in_flight"]["id"] == "t42"
    assert fm["in_flight"]["is_done_check"] is True
    assert fm["in_flight"]["addresses"] == ["i1", "i2"]
    assert fm["actions_dispatched"] == 7
    assert [e["phase"] for e in fm["phase_history"]] == ["verifying"]


def test_status_md_rewritten_on_every_save(tmp_path):
    """Every save rewrites the whole view, so the file never lags the DB."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle"))
    store.save_status("g", GoalStatus(phase="in_flight",
                                      in_flight=InFlight("devclaw", "start_program", "p9", "program")))
    fm = GoalStore._read_frontmatter((tmp_path / "g" / "STATUS.md").read_text())
    assert fm["phase"] == "in_flight"
    assert fm["in_flight"]["id"] == "p9"


# ---- lazy, idempotent migration of a legacy STATUS.md ----------------------


def _seed_status_md_only(tmp_path, goal_id, status, *, clock=None) -> str:
    """Produce a real STATUS.md for ``status`` via a throwaway store (its own
    private DB), then plant it in a FRESH goals dir that has NO DB row — the
    'legacy goal on disk, never migrated' starting condition. Returns the fresh
    goals dir."""
    src = tmp_path / "src"
    gen = GoalStore(src, now=clock or Clock())
    gen.save_status(goal_id, status)
    status_md = (src / goal_id / "STATUS.md").read_text()
    gen._state.close()

    dest = tmp_path / "dest"
    (dest / goal_id).mkdir(parents=True)
    (dest / goal_id / "STATUS.md").write_text(status_md)
    return dest


def test_first_load_migrates_status_md_into_the_table(tmp_path):
    dest = _seed_status_md_only(
        tmp_path, "g",
        GoalStatus(phase="in_flight", lifecycle="executing",
                   in_flight=InFlight("devclaw", "start_program", "p1", "program", "build"),
                   actions_dispatched=3),
    )
    store = GoalStore(dest)
    try:
        # Precondition: STATUS.md exists but there is no goal_status row yet.
        assert store._goal_state.has_status("g") is False

        migrated = store.load_status("g")
        assert migrated.phase == "in_flight"
        assert migrated.lifecycle == "executing"
        assert migrated.actions_dispatched == 3
        assert migrated.in_flight is not None and migrated.in_flight.id == "p1"
        assert migrated.in_flight.ref_kind == "program"

        # Postcondition: the row now exists (migration inserted it).
        assert store._goal_state.has_status("g") is True
    finally:
        store._state.close()


def test_migration_carries_in_flight_and_phase_history(tmp_path):
    # Build a STATUS.md that carries BOTH an in_flight ref and a multi-entry
    # phase_history (accumulated across two saves on the source store).
    clock = Clock()
    src = tmp_path / "src"
    gen = GoalStore(src, now=clock)
    gen.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    clock.advance(60)
    gen.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "implement_feature", "t5", "task", "add /health",
                           addresses=["item-1"]),
    ))
    status_md = (src / "g" / "STATUS.md").read_text()
    gen._state.close()

    dest = tmp_path / "dest"
    (dest / "g").mkdir(parents=True)
    (dest / "g" / "STATUS.md").write_text(status_md)

    store = GoalStore(dest)
    try:
        migrated = store.load_status("g")
        assert migrated.in_flight == InFlight(
            "devclaw", "implement_feature", "t5", "task", "add /health",
            addresses=["item-1"],
        )
        # both phase transitions survived the migration, in order
        assert [e["phase"] for e in migrated.phase_history] == ["idle", "in_flight"]
    finally:
        store._state.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """A second load_status must NOT re-parse / re-migrate — the row-exists
    guard short-circuits before the STATUS.md is touched again."""
    dest = _seed_status_md_only(
        tmp_path, "g", GoalStatus(phase="in_flight", lifecycle="executing"),
    )
    store = GoalStore(dest)
    try:
        parses = {"n": 0}
        orig = GoalStore._parse_status_md

        def _spy(text):
            parses["n"] += 1
            return orig(text)

        monkeypatch.setattr(GoalStore, "_parse_status_md", staticmethod(_spy))

        store.load_status("g")   # migrates → parses the STATUS.md once
        assert parses["n"] == 1
        store.load_status("g")   # row exists → no re-parse, no re-migrate
        store.load_status("g")
        assert parses["n"] == 1

        # And the phase_history table wasn't re-seeded (still one entry).
        hist = store._goal_state.read_phase_history("g")
        assert len(hist) == 1
    finally:
        store._state.close()


def test_save_before_any_load_does_not_drop_status_md_history(tmp_path):
    """save_status on a goal that was never load_status()'d still migrates the
    existing STATUS.md history first, so the append doesn't clobber it."""
    dest = _seed_status_md_only(
        tmp_path, "g",
        GoalStatus(phase="idle", lifecycle="executing"),
    )
    store = GoalStore(dest)
    try:
        # First DB touch is a SAVE (not a load) that flips the phase.
        store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing",
                                          in_flight=InFlight("devclaw", "start_program", "p2", "program")))
        hist = [e["phase"] for e in store.load_status("g").phase_history]
        # The migrated 'idle' entry is preserved AND the new 'in_flight' appended.
        assert hist == ["idle", "in_flight"]
    finally:
        store._state.close()


# ---- corrupt / missing STATUS.md degrade to defaults (never raise) ---------


def test_corrupt_status_md_migrates_to_defaults_without_raising(tmp_path):
    """A truncated STATUS.md (no closing frontmatter fence) is NOT a
    GoalDocCorrupt — status degrades to the default, exactly as pre-PR3."""
    (tmp_path / "g").mkdir(parents=True)
    (tmp_path / "g" / "STATUS.md").write_text("---\nphase: in_flight\n# truncated, no closing fence")

    store = GoalStore(tmp_path)
    try:
        migrated = store.load_status("g")   # must not raise
        assert migrated == GoalStatus()
        # stable on reload (a default row was written for the existing file)
        assert store.load_status("g") == GoalStatus()
    finally:
        store._state.close()


def test_no_status_md_returns_default_and_writes_no_row(tmp_path):
    store = GoalStore(tmp_path)
    try:
        assert store.load_status("never") == GoalStatus()
        # A brand-new goal with no STATUS.md leaves no row — its first
        # save_status creates one.
        assert store._goal_state.has_status("never") is False
    finally:
        store._state.close()


# ---- phase_history accumulates across saves (merge hack removed) -----------


def test_phase_history_accumulates_and_survives_reload(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)

    store.save_status("g", GoalStatus(phase="idle"))
    clock.advance(60)
    store.save_status("g", GoalStatus(phase="in_flight"))
    clock.advance(60)
    store.save_status("g", GoalStatus(phase="in_flight"))   # no change → no entry
    clock.advance(60)
    store.save_status("g", GoalStatus(phase="verifying"))
    clock.advance(60)
    store.save_status("g", GoalStatus(phase="done"))

    hist = store.load_status("g").phase_history
    assert [e["phase"] for e in hist] == ["idle", "in_flight", "verifying", "done"]
    # timestamps are the distinct save times (the injected clock), in order
    ats = [e["at"] for e in hist]
    assert ats == sorted(ats)
    assert len(set(ats)) == 4

    # survives a fresh store over the SAME private DB (persisted table)
    store._state.close()
    reopened = GoalStore(tmp_path)
    try:
        hist2 = reopened.load_status("g").phase_history
        assert [e["phase"] for e in hist2] == ["idle", "in_flight", "verifying", "done"]
    finally:
        reopened._state.close()


def test_two_stores_on_a_shared_statestore_see_live_status(tmp_path):
    """The CLI reads goal status in a separate process from the server. Both
    must resolve to the SAME devclaw.db (via the state= seam) so the CLI shows
    LIVE status. A GoalStore wired to a shared StateStore must see updates
    written by another GoalStore on that same store — and keep seeing the
    latest, never a pinned first-read snapshot. (Regression: T1/PR3 wired
    service.py but not cli.py, so `devclaw projects list` self-created a private
    .goal-state.db, migrated once from the STATUS.md view, then the has_status
    guard pinned that stale snapshot.)"""
    from devclaw.state_store import StateStore

    db = str(tmp_path / "devclaw.db")
    goals = str(tmp_path / "goals")
    shared_a = StateStore(db)
    shared_b = StateStore(db)
    server = GoalStore(goals, now=Clock(), state=shared_a)   # the writer (heartbeat)
    reader = GoalStore(goals, now=Clock(), state=shared_b)   # the CLI, same DB

    server.save_status("g", GoalStatus(phase="executing", next="first"))
    assert reader.load_status("g").phase == "executing"      # live, not empty

    server.save_status("g", GoalStatus(phase="blocked", next="second"))
    got = reader.load_status("g")
    assert got.phase == "blocked" and got.next == "second"   # latest, not pinned
    shared_a.close(); shared_b.close()


# ---- blocked_kind column — lazy schema migration (F8 prerequisite) ----------


def test_blocked_kind_lazy_migration_pre_column_row_reads_empty(tmp_path):
    """A ``goal_status`` row written before the ``blocked_kind`` column
    existed must keep working on the next open: ``GoalState._bootstrap``'s
    forward-compat ALTER adds the column (same lazy pattern as the PR2→PR3
    phase/lifecycle ALTERs), the pre-existing row reads ``blocked_kind == ""``
    (NULL → unclassified), and subsequent full write-path saves round-trip a
    real kind."""
    from devclaw.goal.state import GoalState
    from devclaw.state_store import StateStore

    db_path = str(tmp_path / "devclaw.db")
    store = StateStore(db_path)
    with store._lock:
        # Recreate goal_status with the PRE-blocked_kind schema and seed a
        # blocked row, simulating a DB bootstrapped before this column landed.
        store._db.execute("DROP TABLE IF EXISTS goal_status")
        store._db.execute(
            """
            CREATE TABLE goal_status (
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
            )
            """
        )
        store._db.execute(
            "INSERT INTO goal_status (goal_id, version, state, phase, lifecycle, blocked_on) "
            "VALUES ('g', 3, 'BLOCKED', 'blocked', 'executing', 'which DB?')"
        )
        store._commit()

    gs = GoalState(store)  # _bootstrap runs the blocked_kind ALTER

    back = gs.read_status("g")
    assert back.blocked_kind == ""                     # NULL column → unclassified
    assert back.phase == "blocked" and back.blocked_on == "which DB?"
    assert back.version == 3                           # the row itself is untouched

    # The migrated row keeps working through the full store write path.
    gstore = GoalStore(tmp_path / "goals", now=Clock(), state=store)
    gstore.save_status("g", GoalStatus(phase="blocked", blocked_on="boom", blocked_kind="bug"))
    assert gstore.load_status("g").blocked_kind == "bug"
    store.close()
