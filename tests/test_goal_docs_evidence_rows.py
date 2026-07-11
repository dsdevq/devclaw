"""Tranche 1 / PR6 — checklist.yaml / firmed-draft.yaml move onto ``goal_docs``
rows (the torn-write class T0.4 hardened the file view against becomes
structurally impossible once a goal has a row); log.md / deliveries.md move
onto ``goal_log`` / ``goal_deliveries`` rows, with the files as generated
mirrors (same pattern PR3's STATUS.md and PR5's steering rows use).
``append_delivery`` gains a nullable ``ref_id`` idempotency key —
``UNIQUE(goal_id, ref_id)`` + INSERT OR IGNORE — closing a PR4-review nuance:
a ``TransitionConflict`` landing in the settle-retry window could make the
tick's retry append the SAME delivery twice.

Named regression tests, each with a one-line comment naming the failure class
it closes. See ``devclaw/goal/store.py`` (log/deliveries/checklist/firmed
sections + ``GoalDocCorrupt``), ``devclaw/goal/state.py`` (the ``goal_log`` /
``goal_deliveries`` / ``goal_docs`` row surface), and ``devclaw/goal/tick.py``'s
``_resolve_polling_action`` (the ``ref_id=ref.id`` call-site fix)."""

from __future__ import annotations

import pytest

from devclaw.goal.checklist import dump_checklist
from devclaw.goal.firmed import FirmedGoal, SuccessCriterion, dump_firmed
from devclaw.goal.models import Checklist, ChecklistItem
from devclaw.goal.state import GoalState
from devclaw.goal.store import GoalDocCorrupt, GoalStore
from devclaw.state_store import StateStore
from tests.goal_fakes import Clock, seed_goal


def _example_checklist() -> Checklist:
    return Checklist(
        items=[
            ChecklistItem(
                id="scaffold",
                requirement="Create the csproj.",
                evidence_target="backend/src/Foo.csproj",
                addresses_files=["backend/src/Foo.csproj"],
            ),
        ],
        open_questions=[],
        notes=[],
    )


def _example_firmed() -> FirmedGoal:
    return FirmedGoal(
        status="firmed", round=1, intent="x",
        success_criteria=[SuccessCriterion(id="c1", text="clause")],
    )


def _peek_goal_docs(store: GoalStore, goal_id: str) -> list[str]:
    """Every kind with a row for ``goal_id`` — used to assert a corrupt
    LEGACY file was never ingested (no row appears)."""
    with store._state._lock:
        rows = store._state._db.execute(
            "SELECT kind FROM goal_docs WHERE goal_id = ?", (goal_id,)
        ).fetchall()
    return [r["kind"] for r in rows]


def _peek_log_row_count(store: GoalStore, goal_id: str) -> int:
    with store._state._lock:
        row = store._state._db.execute(
            "SELECT COUNT(*) AS n FROM goal_log WHERE goal_id = ?", (goal_id,)
        ).fetchone()
    return row["n"]


def _peek_delivery_row_count(store: GoalStore, goal_id: str) -> int:
    with store._state._lock:
        row = store._state._db.execute(
            "SELECT COUNT(*) AS n FROM goal_deliveries WHERE goal_id = ?", (goal_id,)
        ).fetchone()
    return row["n"]


# ---- 1. duplicate delivery is impossible (THE headline test) --------------


def test_duplicate_delivery_is_impossible(tmp_path):
    """The PR4-review nuance this PR closes: a settle re-run against the SAME
    in-flight ref (e.g. a TransitionConflict landing in the settle window,
    then the tick's retry re-executing the settle) must not double-record the
    delivery. Two calls with the same ref_id yield ONE row, ONE '## [' section
    in deliveries.md, and recent_deliveries shows it exactly once."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")

    store.append_delivery("g", "add /health", "PR: #7\nVerify: PASSED", ref_id="t-1")
    store.append_delivery("g", "add /health", "PR: #7\nVerify: PASSED", ref_id="t-1")

    assert _peek_delivery_row_count(store, "g") == 1
    deliveries_md = (tmp_path / "g" / "deliveries.md").read_text()
    assert deliveries_md.count("## [") == 1
    assert deliveries_md.count("add /health") == 1
    recent = store.recent_deliveries("g")
    assert recent.count("## [") == 1
    assert recent.count("add /health") == 1

    # A DIFFERENT ref_id is a genuinely new delivery — not swallowed by the
    # dedupe key.
    store.append_delivery("g", "add logging", "PR: #8", ref_id="t-2")
    assert _peek_delivery_row_count(store, "g") == 2
    assert (tmp_path / "g" / "deliveries.md").read_text().count("## [") == 2

    # No ref_id (the pre-PR6 default / callers that never settle against a
    # ref) always inserts — no idempotency key means no dedup.
    store.append_delivery("g", "unrelated work", "PR: #9")
    store.append_delivery("g", "unrelated work", "PR: #9")
    assert _peek_delivery_row_count(store, "g") == 4


# ---- 2. byte-parity on migration (log) -------------------------------------


def test_log_byte_parity_on_migration(tmp_path):
    """A legacy log.md (pre-PR6, no goal_log rows) reads back byte-identical
    through the row-backed recent_log — computed against a reference derived
    straight from the file content, the same filter the pre-PR6 file-tail
    read used (lines starting '- ['). The stray non-'- [' line proves the
    filter still applies post-migration. A second call must not duplicate
    rows — the migration is a true one-shot."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    d = tmp_path / "g"
    d.mkdir(exist_ok=True)
    lines = [
        "- [2026-01-01T00:00:00+00:00] first",
        "- [2026-01-01T00:00:01+00:00] second",
        "- [2026-01-01T00:00:02+00:00] third",
        "not-a-log-line — should be filtered out",
        "- [2026-01-01T00:00:03+00:00] fourth",
        "- [2026-01-01T00:00:04+00:00] fifth",
    ]
    (d / "log.md").write_text("# g — log\n\n" + "\n".join(lines) + "\n")

    # Reference computed directly from the file — exactly the pre-PR6 read.
    ref_lines = [ln for ln in lines if ln.startswith("- [")]
    expected_recent = "\n".join(ref_lines[-3:])

    assert store.recent_log("g", n=3) == expected_recent
    # log_contains-equivalent checks (PR8 retired log_contains — adapted onto
    # recent_log with n wide enough to cover all 5 real rows).
    full_log = store.recent_log("g", n=10)
    assert "third" in full_log
    assert "not-a-log-line" not in full_log  # filtered, never ingested
    assert "nonexistent needle" not in full_log

    assert _peek_log_row_count(store, "g") == 5  # the 5 real log lines, not the stray one

    # Second call must not re-ingest / duplicate rows.
    store.recent_log("g", n=3)
    store.recent_log("g", n=10)
    assert _peek_log_row_count(store, "g") == 5


# ---- 3. byte-parity on migration (deliveries) ------------------------------


def test_deliveries_byte_parity_on_migration(tmp_path):
    """A legacy deliveries.md (pre-PR6, no goal_deliveries rows) reads back
    byte-identical through recent_deliveries — including a SMALL chars bound
    that slices mid-text, matching the pre-PR6 ``text[-chars:]`` behavior
    exactly."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    d = tmp_path / "g"
    d.mkdir(exist_ok=True)
    header = "# g — deliveries (what each action shipped)\n\n"
    section1 = "## [2026-01-01T00:00:00+00:00] add /health\n\nPR: #7\nVerify: PASSED\n\n"
    section2 = "## [2026-01-01T00:00:01+00:00] add logging\n\nPR: #8\n\n"
    legacy_text = header + section1 + section2
    (d / "deliveries.md").write_text(legacy_text)

    assert store.recent_deliveries("g") == legacy_text  # under the default 8000-char bound

    # A tiny char bound must slice mid-text identically to the old file read.
    small = 30
    expected_small = legacy_text[-small:]
    assert store.recent_deliveries("g", chars=small) == expected_small
    assert len(expected_small) < len(legacy_text)  # confirm the bound actually bites

    assert _peek_delivery_row_count(store, "g") == 2  # one row per '## [' section

    # Idempotent — a second read doesn't re-ingest / duplicate rows.
    store.recent_deliveries("g")
    assert _peek_delivery_row_count(store, "g") == 2


# ---- 4. checklist / firmed-draft round-trip through the DB ----------------


def test_checklist_roundtrip_through_db_survives_hand_deleted_file(tmp_path):
    """write -> read equality; the yaml view matches the DB content; a
    hand-deleted checklist.yaml does NOT lose the contract — the DB row wins,
    because the torn-write class the file used to be exposed to is dead."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    cl = _example_checklist()

    store.write_checklist("g", cl)
    assert store.read_checklist("g") == cl
    file_text = (tmp_path / "g" / "checklist.yaml").read_text()
    assert file_text == dump_checklist(cl)

    (tmp_path / "g" / "checklist.yaml").unlink()
    assert store.read_checklist("g") == cl  # DB row wins — the file was only ever a view


def test_firmed_draft_roundtrip_through_db_survives_hand_deleted_file(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    firmed = _example_firmed()

    store.write_firmed_draft("g", firmed)
    assert store.read_firmed_draft("g") == firmed
    file_text = (tmp_path / "g" / "firmed-draft.yaml").read_text()
    assert file_text == dump_firmed(firmed)

    (tmp_path / "g" / "firmed-draft.yaml").unlink()
    assert store.read_firmed_draft("g") == firmed


# ---- 5. corrupt LEGACY file still blocks loudly ----------------------------


def test_corrupt_legacy_checklist_still_blocks_loudly_and_is_never_ingested(tmp_path):
    """No DB row + a garbled checklist.yaml: read_checklist raises
    GoalDocCorrupt (on_corrupt='none' degrades to None); the corrupt file
    must NEVER be ingested into goal_docs — a torn contract must not become
    'migrated' truth."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")

    with pytest.raises(GoalDocCorrupt) as excinfo:
        store.read_checklist("g")
    assert excinfo.value.goal_id == "g"
    assert excinfo.value.doc == "checklist.yaml"
    assert store.read_checklist("g", on_corrupt="none") is None
    assert "checklist" not in _peek_goal_docs(store, "g")  # never laundered into the DB


def test_corrupt_legacy_firmed_draft_still_blocks_loudly_and_is_never_ingested(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    (tmp_path / "g" / "firmed-draft.yaml").write_text("status: [garbage\n")

    with pytest.raises(GoalDocCorrupt) as excinfo:
        store.read_firmed_draft("g")
    assert excinfo.value.goal_id == "g"
    assert excinfo.value.doc == "firmed-draft.yaml"
    assert store.read_firmed_draft("g", on_corrupt="none") is None
    assert "firmed_draft" not in _peek_goal_docs(store, "g")


def test_corrupt_db_row_content_keeps_the_t04_split(tmp_path):
    """The 'should be impossible' path: SQLite's atomic upsert means a
    goal_docs row can't be torn by a crash, but a hand-poked garbled row must
    keep the T0.4 contract — the DEFAULT (cognition/gating) read raises
    GoalDocCorrupt so the tick blocks loudly, while on_corrupt='none' (the
    display paths — get_goal/tail_goal are documented as 'a dashboard read
    must never 500 over it') degrades to None. The tick's default read is
    the loud channel; the dashboard stays readable while the owner
    diagnoses."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store._goal_state.write_doc("g", "checklist", "not yaml: [garbage\n", 1)

    with pytest.raises(GoalDocCorrupt):
        store.read_checklist("g")
    assert store.read_checklist("g", on_corrupt="none") is None


# ---- 6. log append keeps mirror + rows in lockstep -------------------------


def test_log_append_keeps_mirror_and_rows_in_lockstep(tmp_path):
    """After several appends, log.md's content equals the header plus the
    joined row messages — the file mirror and the row-backed source of truth
    never drift apart on the normal (non-crash) append path."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.append_log("g", "first")
    store.append_log("g", "second")
    store.append_log("g", "third")

    with store._state._lock:
        rows = store._state._db.execute(
            "SELECT message FROM goal_log WHERE goal_id = ? ORDER BY id ASC", ("g",)
        ).fetchall()
    messages = [r["message"] for r in rows]
    assert len(messages) == 3

    expected = "# g — log\n\n" + "".join(f"{m}\n" for m in messages)
    assert (tmp_path / "g" / "log.md").read_text() == expected


# ---- deliveries idempotency survives the file-mirror skip on ignore -------


def test_legacy_not_null_ref_id_schema_migrates_to_nullable(tmp_path):
    """DEVIATION-CLOSING regression: goal_deliveries was created with
    ``ref_id TEXT NOT NULL`` back in PR2 (before anything ever wrote to the
    table) — not the nullable column the brief describes. A DB bootstrapped
    by that PR2-era schema must have ``ref_id`` migrated to nullable on the
    next ``GoalState`` construction (SQLite has no in-place ALTER for
    dropping NOT NULL, so this is the copy/drop/rename dance), an existing
    row must survive the migration untouched, and a subsequent NULL-ref_id
    insert (the plain, non-idempotent append path) must succeed rather than
    raise an IntegrityError."""
    db_path = str(tmp_path / "devclaw.db")
    store = StateStore(db_path)
    with store._lock:
        store._db.execute("DROP TABLE IF EXISTS goal_deliveries")
        store._db.execute(
            """
            CREATE TABLE goal_deliveries (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              goal_id     TEXT NOT NULL,
              ref_id      TEXT NOT NULL,
              instruction TEXT,
              body        TEXT,
              created_at  INTEGER NOT NULL,
              UNIQUE(goal_id, ref_id)
            )
            """
        )
        store._db.execute(
            "INSERT INTO goal_deliveries (goal_id, ref_id, instruction, body, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("g1", "old-ref", "old instruction", "old body", 123),
        )
        store._commit()

    gs = GoalState(store)  # _bootstrap runs the nullable-ref_id migration

    info = store._db.execute("PRAGMA table_info(goal_deliveries)").fetchall()
    ref_id_col = next(r for r in info if r["name"] == "ref_id")
    assert ref_id_col["notnull"] == 0  # NOT NULL is gone

    surviving = store._db.execute(
        "SELECT * FROM goal_deliveries WHERE goal_id = 'g1' AND ref_id = 'old-ref'"
    ).fetchone()
    assert surviving is not None and surviving["instruction"] == "old instruction"

    assert gs.append_delivery_row("g1", None, "new block", 456) is True
    store.close()


def test_duplicate_delivery_ref_id_writes_nothing_new_to_file_or_row(tmp_path):
    """A duplicate ref_id must skip BOTH the row insert and the file mirror —
    never a section with no backing row, and never a row with no section."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.append_delivery("g", "ship it", "done", ref_id="only-once")
    before = (tmp_path / "g" / "deliveries.md").read_text()

    store.append_delivery("g", "ship it AGAIN", "done again", ref_id="only-once")

    after = (tmp_path / "g" / "deliveries.md").read_text()
    assert after == before  # nothing appended — the retry was a true no-op
    assert _peek_delivery_row_count(store, "g") == 1
