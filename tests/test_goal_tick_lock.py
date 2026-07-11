"""Tranche 1 / PR8 — the per-goal ``asyncio.Lock`` around ``tick_goal``.

CAS (``GoalStore.transition``'s optimistic-concurrency check, Tranche 1/PR4)
already guarantees CORRECTNESS when two ticks race the SAME goal — the ONLY
same-goal concurrency left standing after PR4 is an MCP-driven ``tick_one``
(manual poke, ops-agent) overlapping the heartbeat's ``tick_all`` sweep for
that goal. Pre-PR8, that race meant BOTH ticks ran a full cognition round (a
planner/evaluator call can take minutes) and the LOSER abandoned its entire
planning round to a ``TransitionConflict`` — correct, but wasteful and a
confusing trace. PR8's lock adds EFFICIENCY + LEGIBILITY on top of CAS: the
second tick simply waits for the first to finish, then reads FRESH state.

Named regression tests, each with a one-line comment naming the property it
proves. See ``devclaw/goal/tick.py``'s ``_TICK_LOCKS`` / ``_tick_lock`` /
``tick_goal`` (the lock's own comment has the full rationale, including WHY
``steer_goal`` / ``cancel_goal`` / ``evaluate_goal`` stay lock-free).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from devclaw.goal import store as store_mod
from devclaw.goal.models import GoalStatus
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from devclaw.goal.transitions import Event, State
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

SLEEP = json.dumps({"decision": "sleep", "note": "waiting"})


def _store(tmp_path, clock):
    return GoalStore(tmp_path, now=clock)


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, eval_every=99):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="http://relay", prepare_ws=fake_prepare, eval_every=eval_every,
    )


class _ParkingPlanner:
    """A planner caller that counts calls, then PARKS on a shared
    ``asyncio.Event`` before returning a fixed decision. Once ``release`` is
    set it stays set (``asyncio.Event`` semantics), so any call that starts
    AFTER the release is a no-op wait — this is what lets one instance model
    both "the first call parks" and "a later call returns immediately"."""

    def __init__(self, release: asyncio.Event, response: str = SLEEP) -> None:
        self.release = release
        self.response = response
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        await self.release.wait()
        return self.response


async def _let_tasks_run(n: int = 25) -> None:
    """Pump the event loop so every currently-schedulable task advances to
    its next await point, without actually resolving anything ourselves.
    Safe to over-call: a task parked on an unset Event or blocked acquiring
    a held Lock simply stays parked/blocked — extra iterations can't cause a
    false pass."""
    for _ in range(n):
        await asyncio.sleep(0)


# ---- 1. same-goal ticks serialize (THE headline test) ----------------------


@pytest.mark.asyncio
async def test_same_goal_ticks_serialize(tmp_path):
    """Two concurrent tick_goal calls for the SAME goal — modeling an
    MCP-driven tick_one racing the heartbeat's tick_all, the one same-goal
    race PR4's CAS leaves standing. Pre-PR8 both would call the planner in
    parallel and the second writer would lose its whole round to
    Outcome.CONFLICT (see devclaw/goal/tick.py's _tick_lock comment — this
    is deliberately demonstrated only in prose here, not as a second code
    path, since removing the lock would just be reverting this PR). With the
    lock: tick2 cannot even acquire it — let alone start cognition — until
    tick1's ENTIRE tick (cognition + its post-plan transition) has finished,
    and neither tick ever sees CONFLICT."""
    store = _store(tmp_path, Clock())
    # cadence="0s": tick2 must run REAL cognition (not just idle at zero cost)
    # for planner.calls == 2 to actually prove serialized-but-not-skipped —
    # last_plan_at is always in the past by the time tick2 gets to plan.
    seed_goal(tmp_path, "g", cadence="0s")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    release = asyncio.Event()
    planner = _ParkingPlanner(release)
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    task1 = asyncio.create_task(_tick(store, "g", planner, evaluator, engine, notifier))
    await _let_tasks_run()
    assert planner.calls == 1  # tick1 reached cognition and is parked on `release`

    task2 = asyncio.create_task(_tick(store, "g", planner, evaluator, engine, notifier))
    await _let_tasks_run()
    # Without the lock, tick2 would have read status and called the planner
    # too (a second, concurrent call) by now. With it, tick2 is blocked
    # acquiring _tick_lock("g") — held by tick1 — and never reaches cognition.
    assert planner.calls == 1

    release.set()
    out1, out2 = await asyncio.gather(task1, task2)

    assert planner.calls == 2                 # both ticks eventually ran real cognition
    assert out1 is Outcome.SLEPT
    assert out2 is Outcome.SLEPT
    assert Outcome.CONFLICT not in (out1, out2)


# ---- 2. different goals do not serialize -----------------------------------


@pytest.mark.asyncio
async def test_different_goals_do_not_serialize(tmp_path):
    """The lock is per-goal, not global — two DIFFERENT goals ticking
    concurrently must both reach cognition in parallel; one goal's tick must
    never wait behind another goal's in-flight planner call."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g1")
    seed_goal(tmp_path, "g2")
    store.save_status("g1", GoalStatus(phase="idle", lifecycle="executing"))
    store.save_status("g2", GoalStatus(phase="idle", lifecycle="executing"))

    release = asyncio.Event()
    planner1 = _ParkingPlanner(release)
    planner2 = _ParkingPlanner(release)
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    task1 = asyncio.create_task(_tick(store, "g1", planner1, evaluator, engine, notifier))
    task2 = asyncio.create_task(_tick(store, "g2", planner2, evaluator, engine, notifier))
    await _let_tasks_run()

    # BOTH cognitions started before either finished — a different-goal Lock
    # object guards g2, so g1's held lock never blocks it.
    assert planner1.calls == 1
    assert planner2.calls == 1

    release.set()
    out1, out2 = await asyncio.gather(task1, task2)

    assert out1 is Outcome.SLEPT
    assert out2 is Outcome.SLEPT


# ---- 3. the lock does not deadlock with the choke-point catch --------------


@pytest.mark.asyncio
async def test_illegal_transition_releases_the_lock(tmp_path, monkeypatch):
    """PR8 wraps tick_goal's ENTIRE body — including the existing
    IllegalTransition/TransitionConflict choke-point catch — in the per-goal
    lock. This proves that catch does not leave the lock held: force an
    IllegalTransition the same way test_goal_transitions.py's regression
    does (yank the real (EXECUTING_IDLE, RESUME_IDLE) edge out of LEGAL,
    modeling 'the table is missing a real code path'); tick1 force-blocks
    internally and returns Outcome.BLOCKED normally (not an unhandled
    raise). A SECOND, independent tick_goal call for the SAME goal —
    awaited right after, bounded by asyncio.wait_for so a real regression
    fails loud instead of hanging the suite — must then run real cognition
    and complete; a leaked lock would hang it forever."""
    store = _store(tmp_path, Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    real_legal = dict(store_mod.LEGAL)
    patched = dict(store_mod.LEGAL)
    del patched[(State.EXECUTING_IDLE, Event.RESUME_IDLE)]
    monkeypatch.setattr(store_mod, "LEGAL", patched)

    planner1 = FakeClaude(SLEEP)
    evaluator = FakeClaude()
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out1 = await asyncio.wait_for(
        _tick(store, "g", planner1, evaluator, engine, notifier), timeout=5,
    )
    assert out1 is Outcome.BLOCKED             # the internal catch fired, not an unhandled raise
    assert planner1.calls == 1                  # cognition ran before the illegal write blew up

    monkeypatch.setattr(store_mod, "LEGAL", real_legal)  # restore — the modeled bug is one-shot
    s = store.load_status("g")
    store.transition("g", Event.UNBLOCK, replace(s, phase="idle", actions_dispatched=0), expect=s)

    planner2 = FakeClaude(SLEEP)
    out2 = await asyncio.wait_for(
        _tick(store, "g", planner2, evaluator, engine, notifier), timeout=5,
    )

    assert out2 is Outcome.SLEPT
    assert planner2.calls == 1                  # a second, independent tick actually ran cognition
