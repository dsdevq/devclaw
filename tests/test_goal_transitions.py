"""The transition choke point — Tranche 1/PR4. Named regression tests, each
closing one specific failure class the choke point exists to prevent. See
``devclaw/goal/transitions.py`` (the LEGAL table + State/Event enums) and
``GoalStore.transition`` / ``.update_status_fields`` / ``.force_block`` in
``devclaw/goal/store.py``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from devclaw.goal import store as store_mod
from devclaw.goal.models import GoalStatus, InFlight
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.goal.transitions import (
    LEGAL,
    Event,
    IllegalTransition,
    State,
    TransitionConflict,
    derive_state,
)
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

ACT = json.dumps(
    {"decision": "act", "note": "ship next", "actions": [{"tool": "start_program", "goal": "build /health"}]}
)
SLEEP = json.dumps({"decision": "sleep", "note": "waiting"})


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, eval_every=99):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare, eval_every=eval_every,
    )


# ---- 1. LEGAL table sanity --------------------------------------------------
# Structural invariants of the table itself — independent of any store/tick
# wiring. A future State/Event addition that violates one of these silently
# reintroduces the "goal wedged with no escape hatch" class of bug.


def test_terminal_states_have_no_outgoing_events():
    """DONE/CANCELLED are terminal — a stray edge FROM either would let a
    finished goal keep mutating."""
    froms = {from_state for (from_state, _event) in LEGAL}
    assert State.DONE not in froms
    assert State.CANCELLED not in froms


def test_block_and_cancel_legal_from_every_non_terminal_state():
    """Every non-terminal state must be able to BLOCK or CANCEL — the escape
    hatch a new state must never forget to wire up."""
    non_terminal = [s for s in State if s not in (State.DONE, State.CANCELLED)]
    for s in non_terminal:
        assert (s, Event.BLOCK) in LEGAL, f"{s.value} is missing a BLOCK edge"
        assert (s, Event.CANCEL) in LEGAL, f"{s.value} is missing a CANCEL edge"


def test_every_non_terminal_state_has_at_least_one_outgoing_event():
    """A non-terminal state with zero outgoing edges would silently wedge any
    goal that reaches it — nothing could ever transition it anywhere."""
    non_terminal = [s for s in State if s not in (State.DONE, State.CANCELLED)]
    froms = {from_state for (from_state, _event) in LEGAL}
    for s in non_terminal:
        assert s in froms, f"{s.value} has no outgoing events at all"


def test_every_state_is_a_legal_target_or_a_reachable_initial_state():
    """Every State member must be either the target of some real edge, or one
    of the two states production code actually starts a goal in — else it's
    dead code nothing ever produces or reaches."""
    targets = {t for target_set in LEGAL.values() for t in target_set}
    # The two shapes production code stamps BEFORE any transition() call ever
    # runs: a brand-new/legacy default (GoalStatus()) and create_goal's
    # explicit "investigating" stamp.
    initial = {derive_state(GoalStatus()), derive_state(GoalStatus(lifecycle="investigating"))}
    for s in State:
        assert s in targets or s in initial, f"{s.value} is neither a legal target nor reachable initially"


# ---- 2. derive_state totality + projection ----------------------------------


@pytest.mark.parametrize(
    "phase,lifecycle,in_flight",
    [
        ("idle", None, None),  # legacy goal, no lifecycle stamped
        ("idle", "executing", None),
        ("idle", "investigating", None),
        ("idle", "firming", None),
        ("in_flight", "executing", "action"),
        ("in_flight", "investigating", "discovery"),
        ("verifying", "executing", "done_check"),
        ("blocked", "executing", None),
        ("blocked", "firming", None),
        ("blocked", "investigating", None),
        ("blocked", "executing", "action"),  # preserved ref — corrupt-doc / lost-ref block
        ("done", "executing", None),
        ("done", None, "action"),  # done wins even with a stale in_flight
        ("cancelled", "executing", None),
    ],
)
def test_derive_state_is_total(phase, lifecycle, in_flight):
    """derive_state must never raise and must always return a State member,
    for every field combination production code (or a legacy row) can hold."""
    ref = None
    if in_flight == "action":
        ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it")
    elif in_flight == "discovery":
        ref = InFlight("devclaw", "review_repository", "t1", "task", "look", is_discovery=True)
    elif in_flight == "done_check":
        ref = InFlight("devclaw", "review_repository", "t1", "task", "verify", is_done_check=True)
    status = GoalStatus(phase=phase, lifecycle=lifecycle, in_flight=ref)
    result = derive_state(status)
    assert isinstance(result, State)


def test_derive_state_blocked_plus_firming_lifecycle_is_firming_blocked():
    assert derive_state(GoalStatus(phase="blocked", lifecycle="firming")) is State.FIRMING_BLOCKED


def test_derive_state_blocked_wins_over_a_preserved_in_flight_ref():
    """A blocked goal may carry a preserved in_flight ref (the corrupt-doc /
    lost-ref block handlers deliberately keep it so it settles normally once
    the block clears) — blocked-ness must win for the derived STATE, matching
    _classify's real dispatch behavior for a blocked goal."""
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it")
    status = GoalStatus(phase="blocked", lifecycle="executing", in_flight=ref)
    assert derive_state(status) is State.BLOCKED


# ---- 3. illegal transition raises, writes nothing ---------------------------


def test_transition_illegal_raises_and_writes_nothing(tmp_path):
    """store.transition() with an event only legal from a different FROM state
    raises IllegalTransition and leaves the row untouched (no version bump)."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))  # → executing_idle
    before = store.load_status("g")

    with pytest.raises(IllegalTransition):
        # ACTION_SETTLED is only legal from action_in_flight, not executing_idle.
        store.transition("g", Event.ACTION_SETTLED, replace(before, phase="idle"), expect=before)

    after = store.load_status("g")
    assert after.version == before.version
    assert after.phase == "idle"


# ---- 4. illegal transition at tick force-blocks + notifies once ------------


@pytest.mark.asyncio
async def test_illegal_transition_at_tick_force_blocks_and_notifies_once(tmp_path, monkeypatch):
    """A handler proposing a transition the LEGAL table doesn't permit (here:
    the real (executing_idle, resume_idle) edge the planner's 'sleep' decision
    needs is yanked out, modeling 'the table is missing a real code path')
    must not crash-loop the tick: tick_goal force-blocks the goal, pings the
    owner exactly once, and the goal is Outcome.BLOCKED — never an unhandled
    exception."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    real_legal = dict(LEGAL)
    patched = dict(LEGAL)
    del patched[(State.EXECUTING_IDLE, Event.RESUME_IDLE)]
    monkeypatch.setattr(store_mod, "LEGAL", patched)

    planner = FakeClaude(SLEEP)
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    saved = store.load_status("g")
    assert saved.phase == "blocked"
    assert "illegal state transition" in (saved.blocked_on or "")
    owner_pings = [m for m in notifier.sent if "internal state error" in m]
    assert len(owner_pings) == 1

    # The NEXT tick idles quietly — no unread steering, so a blocked goal
    # never even reaches the planner (0 tokens), let alone re-attempts the
    # same illegal call or pings the owner again. Restore the real table
    # first: the modeled "bug" was one-shot, not a permanent break.
    monkeypatch.setattr(store_mod, "LEGAL", real_legal)
    planner2 = FakeClaude(SLEEP)
    out2 = await _tick(store, "g", planner2, evaluator, engine, notifier)

    assert out2 is Outcome.IDLE
    assert planner2.calls == 0
    assert len(notifier.sent) == 1  # no new ping


# ---- 5. cancel mid-tick is never clobbered (THE headline test) -------------


class _CancelMidAwaitCaller:
    """A planner caller that cancels the goal DURING its own await — models
    cancel_goal landing between the tick's status load and its eventual
    write."""

    def __init__(self, store: GoalStore, goal_id: str) -> None:
        self.store = store
        self.goal_id = goal_id
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        s = self.store.load_status(self.goal_id)
        self.store.transition(
            self.goal_id, Event.CANCEL, replace(s, phase="cancelled", in_flight=None), expect=s,
        )
        return SLEEP


@pytest.mark.asyncio
async def test_cancel_mid_tick_is_never_clobbered(tmp_path):
    """Closes the stale-snapshot un-cancel class: a cancel_goal landing DURING
    the tick's planner await must win — the tick's own (now-stale) write must
    be abandoned, not silently overwrite the cancel."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = _CancelMidAwaitCaller(store, "g")
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.CONFLICT
    assert planner.calls == 1
    final = store.load_status("g")
    assert final.phase == "cancelled"
    assert final.in_flight is None


# ---- 6. steer-unblock mid-tick abandons cleanly -----------------------------


class _SteerUnblockMidAwaitCaller:
    """A planner caller that appends steering AND unblocks the goal DURING its
    own await — models a second steer_goal call landing mid-tick."""

    def __init__(self, store: GoalStore, goal_id: str) -> None:
        self.store = store
        self.goal_id = goal_id
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.store.append_steering(self.goal_id, ["do X instead"], source="denys")
        s = self.store.load_status(self.goal_id)
        if s.phase == "blocked":
            self.store.transition(
                self.goal_id, Event.UNBLOCK,
                replace(s, phase="idle", actions_dispatched=0), expect=s,
            )
        return SLEEP


@pytest.mark.asyncio
async def test_steer_unblock_mid_tick_abandons_cleanly(tmp_path):
    """A blocked goal already has unread steering (so it plans this tick); a
    SECOND steer+unblock lands mid-await. The tick's own write is abandoned
    (Outcome.CONFLICT) rather than clobbering the unblock, and the NEXT tick
    plans with ALL the steering visible (nothing was silently consumed by the
    abandoned write)."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(phase="blocked", lifecycle="executing", blocked_on="waiting on owner"),
    )
    store.append_steering("g", ["first steer"], source="denys")

    planner = _SteerUnblockMidAwaitCaller(store, "g")
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.CONFLICT
    assert planner.calls == 1
    mid = store.load_status("g")
    assert mid.phase == "idle"  # the mid-tick unblock stuck
    # PR5: consumption is by exact goal_steering row id (rides the post-plan
    # transition), not a cursor — the abandoned tick's consume_steering never
    # landed (it rode the same failed CAS as the decision write), so BOTH
    # lines are still unconsumed. Mechanically adapted from the pre-PR5
    # `mid.inbox_cursor == 0` assertion; same intent.
    unconsumed = [line for _, line in store.unread_steering_rows("g")]
    assert unconsumed == ["first steer", "do X instead"]

    planner2 = FakeClaude(SLEEP)
    out2 = await _tick(store, "g", planner2, evaluator, engine, notifier)

    assert planner2.calls == 1  # real cognition fired — steering IS work
    assert "first steer" in planner2.last_prompt
    assert "do X instead" in planner2.last_prompt


# ---- 7. version threading within one tick -----------------------------------


@pytest.mark.asyncio
async def test_version_threading_watchdog_write_then_dispatch_no_conflict(tmp_path):
    """The no-progress watchdog's update_status_fields() write (an early,
    same-tick write, since last_progress_at is None) bumps the row's version;
    the SAME tick's later dispatch transition() must CAS against the version
    the watchdog RETURNED, not the pre-watchdog snapshot — else a healthy tick
    would spuriously TransitionConflict against its own earlier write."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(ACT)
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.DISPATCHED  # not CONFLICT — the watchdog's write threaded through
    assert len(engine.dispatched) == 1
    saved = store.load_status("g")
    assert saved.phase == "in_flight"


# ---- 8. update_status_fields is column-only ---------------------------------


def test_update_status_fields_is_column_only(tmp_path):
    """A concurrent phase-changing write must survive an update_status_fields
    call that has no idea it happened — the column-only UPDATE physically
    cannot touch phase/lifecycle/in_flight/blocked_on/next, so there's nothing
    to CAS against in the first place."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    stale = store.load_status("g")  # a caller's snapshot, about to go stale

    # A concurrent writer transitions the goal — lands AFTER `stale` was read.
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it")
    store.transition(
        "g", Event.DISPATCH_ACTION,
        replace(stale, phase="in_flight", in_flight=ref, next="working"),
        expect=stale,
    )

    # update_status_fields — no `expect` param exists on it at all — must not
    # clobber the concurrent phase change.
    before_version = store.load_status("g").version
    fresh = store.update_status_fields(
        "g", last_eval_verdict="on_track", last_eval_at="2026-07-11T00:00:00+00:00",
        last_eval_note="looking fine",
    )

    assert fresh.phase == "in_flight"
    assert fresh.in_flight == ref
    assert fresh.next == "working"
    assert fresh.last_eval_verdict == "on_track"
    assert fresh.version == before_version + 1

    # STATUS.md view stays honest (the rollback-path artifact).
    md = (tmp_path / "g" / "STATUS.md").read_text()
    assert "on_track" in md

    with pytest.raises(ValueError):
        store.update_status_fields("g", blocked_on="nope — must go through transition()")


# ---- 9. save_status stamps state; legacy NULL-state rows still work --------


def test_save_status_stamps_derived_state_on_every_write(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    status = GoalStatus(phase="idle", lifecycle="executing")
    store.save_status("g", status)
    saved = store.load_status("g")
    assert saved.state == derive_state(status).value == State.EXECUTING_IDLE.value


def test_legacy_null_state_row_rehydrates_and_first_transition_still_works(tmp_path):
    """A pre-PR4 row (state column never stamped, NULL) must rehydrate with
    state=None — not crash, not a fabricated value — and the FIRST
    transition() against it must still succeed: CAS derives cur_state from the
    business fields (phase/lifecycle/in_flight) when the stored state is
    missing, exactly like a fresh derive_state() call would."""
    store = GoalStore(tmp_path, now=Clock())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    # Simulate a PR3-era row that predates the `state` column being stamped.
    with store._state._lock:
        store._state._db.execute("UPDATE goal_status SET state = NULL WHERE goal_id = ?", ("g",))
        store._state._db.commit()

    legacy = store.load_status("g")
    assert legacy.state is None

    ref = InFlight("devclaw", "implement_feature", "t1", "task", "go")
    result = store.transition(
        "g", Event.DISPATCH_ACTION,
        replace(legacy, phase="in_flight", in_flight=ref),
        expect=legacy,
    )
    assert result.state == State.ACTION_IN_FLIGHT.value
    assert store.load_status("g").state == State.ACTION_IN_FLIGHT.value
