"""The goal-level no-progress watchdog — a zero-token wall-clock that pings the
owner once when an executing goal stops shipping. Complements the per-task timeout
(one hung run) by catching a goal that keeps churning without delivering.

Driven entirely through the public tick_goal with an injected clock + fakes — no
network, no claude. The quota assertion (planner.calls == 0) proves the watchdog
is pure mechanism on the measuring path."""
from __future__ import annotations

from dataclasses import replace

from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from tests.goal_fakes import (
    Clock,
    FakeClaude,
    FakeEngine,
    RecordingNotifier,
    fake_prepare,
    seed_goal,
)

WINDOW = 100  # seconds — small so tests advance the clock past it cheaply


def _executing(store: GoalStore, **over) -> GoalStatus:
    """An executing, cadence-not-due status (last_plan_at = now) so a tick reaches
    the watchdog but does NOT spend planner cognition on the idle path."""
    base = GoalStatus(
        phase="idle", lifecycle="executing",
        last_plan_at=store.now_iso(), last_progress_at=store.now_iso(),
    )
    return replace(base, **over)


async def _tick(store, *, notifier, engine=None, planner=None, window=WINDOW):
    return await tick_goal(
        "g", store=store, engine=engine or FakeEngine(),
        planner_caller=planner or FakeClaude(), evaluator_caller=FakeClaude(),
        notifier=notifier, prepare_ws=fake_prepare,
        no_progress_s=window, eval_every=99,
    )


def _turtles(notifier) -> list[str]:
    return [m for m in notifier.sent if "🐢" in m]


async def test_no_fire_before_window(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", _executing(store))
    clock.advance(WINDOW - 10)  # not yet stalled
    notifier = RecordingNotifier()
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    out = await _tick(store, notifier=notifier, planner=planner)

    assert out is Outcome.IDLE
    assert _turtles(notifier) == []
    assert planner.calls == 0  # zero-token: watchdog never touched cognition


async def test_fires_once_after_window(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", _executing(store))
    clock.advance(WINDOW + 30)  # now stalled
    notifier = RecordingNotifier()
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    out1 = await _tick(store, notifier=notifier, planner=planner)
    assert out1 is Outcome.IDLE
    assert len(_turtles(notifier)) == 1
    assert store.load_status("g").no_progress_notified is True
    assert planner.calls == 0

    # a second stalled tick must NOT re-ping (once per stall)
    clock.advance(WINDOW + 30)
    await _tick(store, notifier=notifier, planner=planner)
    assert len(_turtles(notifier)) == 1


async def test_delivery_resets_the_watchdog(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    # an in-flight action that's already been flagged as stalled
    stale = _executing(
        store, phase="in_flight",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "do x"),
        no_progress_notified=True,
    )
    store.save_status("g", stale)
    clock.advance(WINDOW + 30)
    notifier = RecordingNotifier()
    # the action lands a delivery (gate passed, no PR → no automerge path)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="shipped"))
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    await _tick(store, notifier=notifier, engine=engine, planner=planner)

    s = store.load_status("g")
    assert s.no_progress_notified is False           # reset by the delivery
    assert s.last_progress_at == store.now_iso()     # baseline moved to delivery time

    # fresh stall after the delivery fires again (not silenced forever)
    store.save_status("g", _executing(store, last_progress_at=s.last_progress_at))
    clock.advance(WINDOW + 30)
    notifier2 = RecordingNotifier()
    await _tick(store, notifier=notifier2, planner=planner)
    assert len(_turtles(notifier2)) == 1


async def test_disabled_when_window_zero(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    store.save_status("g", _executing(store))
    clock.advance(10 * WINDOW)
    notifier = RecordingNotifier()
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    await _tick(store, notifier=notifier, planner=planner, window=0)

    assert _turtles(notifier) == []


async def test_skips_goal_waiting_on_owner(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    # blocked = already escalated to the owner; the watchdog must stay quiet
    store.save_status("g", _executing(store, phase="blocked", blocked_on="need a decision"))
    clock.advance(10 * WINDOW)
    notifier = RecordingNotifier()
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    await _tick(store, notifier=notifier, planner=planner)

    assert _turtles(notifier) == []


async def test_self_initializes_baseline_when_missing(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g")
    # a (legacy) executing goal with no progress baseline recorded yet
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing", last_plan_at=store.now_iso(), last_progress_at=None))
    notifier = RecordingNotifier()
    planner = FakeClaude('{"decision":"sleep","note":"idle"}')

    await _tick(store, notifier=notifier, planner=planner)

    # the watchdog stamped a baseline rather than firing on a null reference
    assert store.load_status("g").last_progress_at == store.now_iso()
    assert _turtles(notifier) == []
