"""Tranche 1 / PR5 — goal_steering rows are the source of truth for steering
consumption (``consumed_at IS NULL`` == unread), consumed by EXACT row id via
``GoalStore.transition(..., consume_steering=[...])`` atomically with the
decision the steering informed. ``inbox.md`` stays the human-readable mirror
AND a hand-append input, lazily ingested via a per-goal cursor.

Named regression tests, each with a one-line comment naming the failure class
it closes. See ``devclaw/goal/store.py`` (``_ingest_inbox`` / ``append_steering``
/ ``unread_steering_rows``), ``devclaw/goal/state.py`` (the ``goal_steering``
row surface), and ``devclaw/goal/tick.py``'s ``_handle_executing`` (exact-id
capture + threading ``consume_steering`` into every post-plan transition).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from devclaw.goal.models import GoalStatus
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.goal.transitions import Event
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

SLEEP = json.dumps({"decision": "sleep", "note": "waiting"})


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, eval_every=99):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare, eval_every=eval_every,
    )


# ---- 1. steer-during-planner-await not lost (THE headline test) -----------


class _SteerMidAwaitCaller:
    """A planner caller that appends a FRESH steering line via
    ``store.append_steering`` DURING its own cognition await — models
    ``steer_goal`` landing between the tick's steering-read and its
    eventual post-plan transition. Returns a sleep decision so the tick
    reaches the post-plan transition where consumption would happen."""

    def __init__(self, store: GoalStore, goal_id: str) -> None:
        self.store = store
        self.goal_id = goal_id
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.store.append_steering(self.goal_id, ["mid-await correction"], source="denys")
        return SLEEP


@pytest.mark.asyncio
async def test_steer_during_planner_await_not_lost(tmp_path):
    """THE headline regression (PR5). Pre-PR5, unread steering was consumed
    by a COUNT-based cursor (``inbox_cursor = steering_cursor(goal_id)`` =
    "everything that exists in inbox.md NOW"), stamped AFTER the planner
    call returned — a steer landing during the planner's cognition await
    was silently swallowed by that count even though the planner never saw
    it. Under the old model this test's tick1 CAS would have SUCCEEDED
    (append_steering was file-only, no goal_status write, so `expect=`
    never went stale) and consumed the mid-await line via the blanket
    count, permanently losing it — tick2's planner would never see
    "mid-await correction". PR5 makes append_steering row-backed, which
    ALSO bumps goal_status.version (the ingest-cursor write); the mid-await
    append therefore makes the tick's OWN post-plan transition CAS-fail
    exactly like a concurrent steer_goal call today (Outcome.CONFLICT) —
    the row rides the abandoned write and stays unread either way."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = _SteerMidAwaitCaller(store, "g")
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert planner.calls == 1
    assert out is Outcome.CONFLICT  # the mid-await write made this tick's own CAS stale
    unread = store.unread_steering_rows("g")
    # rows store the mirror-formatted line (`- [denys <ts>] …`) so the
    # planner-visible text keeps its source marker — assert on the payload.
    assert len(unread) == 1 and "mid-await correction" in unread[0][1]

    planner2 = FakeClaude(SLEEP)
    out2 = await _tick(store, "g", planner2, evaluator, engine, notifier)

    assert planner2.calls == 1  # real cognition fired — steering IS work
    assert "mid-await correction" in planner2.last_prompt
    assert out2 is Outcome.SLEPT


# ---- 2. exact-id consumption ------------------------------------------------


def test_exact_id_consumption(tmp_path):
    """Exact-id consumption: two unread rows are read (their ids captured),
    the decision transition consumes precisely those ids; a THIRD row
    appended AFTER the read (simulating a steer landing after the planner
    already snapshotted what to consume) is untouched by that consume —
    it's picked up whole on the next read."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.append_steering("g", ["first", "second"], source="denys")

    rows = store.unread_steering_rows("g")
    assert len(rows) == 2
    assert "first" in rows[0][1] and "second" in rows[1][1]
    ids = [rid for rid, _ in rows]

    # a third steer lands AFTER the read captured its ids
    store.append_steering("g", ["third"], source="denys")

    status = store.load_status("g")
    # `new` MUST be built off the freshly-loaded status (replace(), never a
    # bare GoalStatus()) — same rule every production transition() call site
    # follows, so inbox_cursor (the ingest cursor, unrelated to this
    # consume) isn't clobbered back to 0 by the write.
    store.transition(
        "g", Event.RESUME_IDLE, replace(status, phase="idle", next="done"),
        expect=status, consume_steering=ids,
    )

    remaining = store.unread_steering_rows("g")
    assert len(remaining) == 1 and "third" in remaining[0][1]


# ---- 3. consumption is atomic with the decision write -----------------------


class _BumpVersionMidAwaitCaller:
    """A planner caller that bumps goal_status.version DURING its own await
    via a write UNRELATED to steering — models any concurrent writer (the
    no-progress watchdog, another in-process tick) landing mid-plan."""

    def __init__(self, store: GoalStore, goal_id: str) -> None:
        self.store = store
        self.goal_id = goal_id
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.store.update_status_fields(self.goal_id, last_tick_at=self.store.now_iso())
        return SLEEP


@pytest.mark.asyncio
async def test_consumption_atomic_with_decision_write(tmp_path):
    """Consumption rides the SAME CAS'd transaction as the decision write: a
    version bump mid-plan from something UNRELATED to steering (simulated
    via update_status_fields, the same shape test_goal_transitions.py uses
    for "any writer can trigger TransitionConflict") makes the tick's
    post-plan transition CAS-fail — the steering rows it read stay
    unconsumed (they ride the abandoned write), and the NEXT tick re-plans
    with the SAME steering still visible."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.append_steering("g", ["do the thing"], source="denys")

    planner = _BumpVersionMidAwaitCaller(store, "g")
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.CONFLICT
    unread = store.unread_steering_rows("g")
    assert len(unread) == 1 and "do the thing" in unread[0][1]

    planner2 = FakeClaude(SLEEP)
    out2 = await _tick(store, "g", planner2, evaluator, engine, notifier)
    assert planner2.calls == 1
    assert "do the thing" in planner2.last_prompt
    assert out2 is Outcome.SLEPT


# ---- 4. lazy migration of a pre-PR5 inbox.md --------------------------------


def test_lazy_migration_of_pre_pr5_inbox(tmp_path):
    """Lazy migration: a goal whose stored inbox_ingest_cursor was the OLD
    consume cursor (pre-PR5) must not have its already-consumed history
    re-fed to the planner on the first post-upgrade ingest. Lines below the
    old cursor become CONSUMED rows (preserved for the record, never
    unread); only lines at/after the cursor are fresh. Idempotent: a second
    read changes nothing further."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    d = tmp_path / "g"
    d.mkdir(exist_ok=True)
    (d / "inbox.md").write_text(
        "- [denys 2026-01-01T00:00:00+00:00] line one\n"
        "- [denys 2026-01-01T00:00:01+00:00] line two\n"
        "- [denys 2026-01-01T00:00:02+00:00] line three\n"
        "- [denys 2026-01-01T00:00:03+00:00] line four\n"
        "- [denys 2026-01-01T00:00:04+00:00] line five\n"
    )
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing", inbox_cursor=3))

    rows = store.unread_steering_rows("g")
    assert len(rows) == 2
    assert "line four" in rows[0][1]
    assert "line five" in rows[1][1]

    # peek at the full table to confirm the 3 consumed / 2 unconsumed split
    with store._state._lock:
        peek = store._state._db.execute(
            "SELECT consumed_at FROM goal_steering WHERE goal_id = ? ORDER BY id", ("g",)
        ).fetchall()
    assert [row["consumed_at"] is not None for row in peek] == [True, True, True, False, False]

    # idempotent — a second read yields the same 2 unread rows
    rows2 = store.unread_steering_rows("g")
    assert [line for _, line in rows2] == [line for _, line in rows]


# ---- 5. mirror no-double-ingest ---------------------------------------------


def test_mirror_no_double_ingest(tmp_path):
    """append_steering's own cursor bump means the mirrored inbox.md lines
    are never re-ingested as a SECOND row by a later read. A genuinely NEW
    hand-typed line (appended to the file directly, bypassing
    append_steering) DOES show up — as exactly one additional 'manual'
    row."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.append_steering("g", ["ship the health check"], source="denys")

    rows = store.unread_steering_rows("g")
    assert len(rows) == 1 and "ship the health check" in rows[0][1]
    # calling again must not duplicate the mirrored line
    rows_again = store.unread_steering_rows("g")
    assert [line for _, line in rows_again] == [line for _, line in rows]

    # a genuinely hand-typed line, appended straight to the file
    with (tmp_path / "g" / "inbox.md").open("a") as fh:
        fh.write("- [denys 2026-01-02T00:00:00+00:00] hand-typed note\n")

    rows_after_hand_append = store.unread_steering_rows("g")
    lines = [line for _, line in rows_after_hand_append]
    # machine and hand rows alike hold the inbox.md line verbatim — both keep
    # the `- [source ts]` prefix the planner prompt's [auto-eval]-marker
    # contract relies on.
    assert len(lines) == 2
    assert "ship the health check" in lines[0] and lines[0].startswith("- [denys ")
    assert lines[1] == "- [denys 2026-01-02T00:00:00+00:00] hand-typed note"
    raw_rows = store._goal_state.unread_steering_rows("g")
    assert [r["source"] for r in raw_rows] == ["denys", "manual"]


# ---- 6. ingest tolerates cursor > file length -------------------------------


def test_ingest_tolerates_cursor_past_file_length(tmp_path):
    """The crash-ordering edge documented in GoalStore.append_steering's
    docstring: the ingest cursor ends up AHEAD of inbox.md's current line
    count (an operator clearing/truncating the file by hand is the simplest
    way to reach it). _ingest_inbox must not raise, go negative, or
    re-ingest anything — it's simply a no-op until the file catches back
    up, and existing unconsumed rows are untouched."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.append_steering("g", ["already ingested"], source="denys")
    assert store.load_status("g").inbox_cursor == 1

    # operator clears inbox.md by hand — the stored cursor is now AHEAD of
    # the (now-shorter) file.
    (tmp_path / "g" / "inbox.md").write_text("# g — inbox (steering)\n\n")

    rows = store.unread_steering_rows("g")  # must not raise / go negative
    assert len(rows) == 1 and "already ingested" in rows[0][1]  # the original row is untouched

    assert store.load_status("g").inbox_cursor == 1  # no new lines to account for — a no-op
