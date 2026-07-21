"""Goal-layer quota pause — a usage limit reaching goal cognition pauses the whole
layer (0 tokens) and auto-resumes, instead of crash-looping + burning quota."""
from __future__ import annotations

import pytest

from devclaw.goal.tick import Outcome, tick_all
from devclaw.goal.store import GoalStore
from devclaw.state_store import _now_ms
from tests.goal_fakes import Clock, FakeClaude, RecordingNotifier, fake_prepare, seed_goal


class PausableEngine:
    """Minimal engine double that carries the shared quota pause (like the real
    InProcessEngine) but refuses to dispatch/poll (these tests never get that far)."""

    def __init__(self) -> None:
        self._pause: tuple[int, str] = (0, "")

    def global_pause(self) -> tuple[int, str]:
        return self._pause

    def set_global_pause(self, until_ms: int, reason: str) -> None:
        self._pause = (until_ms, reason)

    def clear_global_pause(self) -> None:
        self._pause = (0, "")

    async def dispatch(self, *a, **k):  # pragma: no cover
        raise AssertionError("must not dispatch while rate-limited")

    async def poll(self, *a, **k):  # pragma: no cover
        raise AssertionError("must not poll while rate-limited")


class RaisingClaude:
    def __init__(self, msg: str) -> None:
        self.msg = msg
        self.calls = 0

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError(self.msg)


async def test_goal_cognition_rate_limit_pauses_layer(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = PausableEngine()
    planner = RaisingClaude("API Error: 429 Too Many Requests")

    out = await tick_all(
        store=store, engine=eng, planner_caller=planner, evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare, eval_every=99,
    )

    assert out["g"] is Outcome.RATE_LIMITED
    until, reason = eng.global_pause()
    assert until > _now_ms() and "rate_limit" in reason


async def test_tick_all_skips_all_cognition_while_paused(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = PausableEngine()
    eng.set_global_pause(_now_ms() + 60_000, "manual")  # paused 60s out
    planner = RaisingClaude("must not be called")

    out = await tick_all(
        store=store, engine=eng, planner_caller=planner, evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare, eval_every=99,
    )

    assert out["g"] is Outcome.RATE_LIMITED
    assert planner.calls == 0  # zero tokens while paused


async def test_expired_pause_clears_and_proceeds(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = PausableEngine()
    eng.set_global_pause(_now_ms() - 1000, "expired")  # in the past
    planner = RaisingClaude("real bug: boom")  # non-limit → ERROR, proves we proceeded

    out = await tick_all(
        store=store, engine=eng, planner_caller=planner, evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare, eval_every=99,
    )

    assert eng.global_pause()[0] == 0          # expired pause was cleared
    assert out["g"] is Outcome.ERROR           # proceeded; the real bug surfaced
    assert planner.calls == 1


# ---- owner ping on pause + resume -------------------------------------------
# A weekly cap can silently halt everything for days — the owner must hear the
# pause ONCE (not every tick) and the resume ONCE. The pinged-flag rides the
# same engine getattr seam the pause itself uses, so fakes without it still work.


class FlaggedPausableEngine(PausableEngine):
    """PausableEngine + the pause_notified flag, like the real InProcessEngine.
    Mirrors the kind-persisting signature: the resume path keys on the kind
    recorded WITH the ping, not the live (clearable) pause reason."""

    def __init__(self) -> None:
        super().__init__()
        self._notified = False
        self._notified_kind = ""

    def pause_notified(self) -> bool:
        return self._notified

    def set_pause_notified(self, on: bool, kind: str = "") -> None:
        self._notified = on
        self._notified_kind = kind if on else ""

    def pause_notified_kind(self) -> str:
        return self._notified_kind


async def _tick(store, eng, notifier, planner=None):
    return await tick_all(
        store=store, engine=eng, planner_caller=planner or FakeClaude(),
        evaluator_caller=FakeClaude(), notifier=notifier, prepare_ws=fake_prepare,
        eval_every=99,
    )


async def test_paused_tick_pings_owner_exactly_once(tmp_path):
    store = GoalStore(tmp_path, now=Clock())  # no goals needed — the gate is fleet-wide
    eng = FlaggedPausableEngine()
    eng.set_global_pause(_now_ms() + 60_000, "quota: You're out of extra usage")
    notifier = RecordingNotifier()

    await _tick(store, eng, notifier)   # first paused tick → the ping
    await _tick(store, eng, notifier)   # still paused → NO second ping

    pings = [m for m in notifier.sent if "paused on a usage limit" in m]
    assert len(pings) == 1
    assert "quota: You're out of extra usage" in pings[0]   # the reason
    assert "resuming ~" in pings[0] and "UTC" in pings[0]   # the computed reset time


async def test_resume_pings_owner_once(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    eng = FlaggedPausableEngine()
    eng.set_global_pause(_now_ms() + 60_000, "quota: weekly cap")
    notifier = RecordingNotifier()

    await _tick(store, eng, notifier)                    # pause ping
    eng.set_global_pause(_now_ms() - 1000, "quota: weekly cap")  # window elapses
    await _tick(store, eng, notifier)                    # resume ping
    await _tick(store, eng, notifier)                    # quiet afterwards

    resumes = [m for m in notifier.sent if "usage limit lifted" in m]
    assert len(resumes) == 1
    assert len(notifier.sent) == 2                       # exactly pause + resume
    assert eng.global_pause()[0] == 0                    # pause cleared too


async def test_resume_pings_even_if_pause_cleared_elsewhere(tmp_path):
    """The task queue lazily clears an expired pause too. When tick_all finds
    no pause but a set flag, the owner still hears the resume — the flag isn't
    lost with the pause."""
    store = GoalStore(tmp_path, now=Clock())
    eng = FlaggedPausableEngine()
    eng.set_global_pause(_now_ms() + 60_000, "quota")
    notifier = RecordingNotifier()

    await _tick(store, eng, notifier)                    # pause ping
    eng.clear_global_pause()                             # the OTHER layer cleared it first
    await _tick(store, eng, notifier)

    assert sum("usage limit lifted" in m for m in notifier.sent) == 1


async def test_fakes_without_flag_accessors_keep_working(tmp_path):
    """PausableEngine has no pause_notified accessors — the seam must degrade
    to a no-op (no crash, still RATE_LIMITED, still zero cognition)."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = PausableEngine()
    eng.set_global_pause(_now_ms() + 60_000, "manual")
    planner = RaisingClaude("must not be called")

    out = await _tick(store, eng, RecordingNotifier(), planner=planner)

    assert out["g"] is Outcome.RATE_LIMITED
    assert planner.calls == 0


# ---- auth failures pause too (2026-07-20 night incident) ---------------------
# An expired VPS login used to classify REAL: ~58 terminal planner failures
# across the whole unattended run window, no pause, no ping. AUTH now rides the
# same pause machinery with an ACTIONABLE ping and a fixed re-probe; expiry
# resumes silently (a re-probe is not "the limit lifted") and re-pings only if
# the login is still broken.

_AUTH_ERR = "Failed to authenticate: OAuth session expired and could not be refreshed"


async def test_goal_cognition_auth_failure_pauses_layer_with_relogin_ping(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = FlaggedPausableEngine()
    notifier = RecordingNotifier()
    planner = RaisingClaude(_AUTH_ERR)

    out = await _tick(store, eng, notifier, planner=planner)  # trips auth → pause
    assert out["g"] is Outcome.RATE_LIMITED
    until, reason = eng.global_pause()
    assert until > _now_ms() and reason.startswith("auth")

    await _tick(store, eng, notifier, planner=planner)  # paused tick → the ping
    await _tick(store, eng, notifier, planner=planner)  # still paused → no second ping
    pings = [m for m in notifier.sent if "re-login" in m]
    assert len(pings) == 1
    assert "auth" in pings[0] and "/login" in pings[0]      # actionable, names the fix
    assert planner.calls == 1                               # zero cognition while paused


async def test_auth_pause_expiry_resumes_silently_and_repings_while_broken(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = FlaggedPausableEngine()
    notifier = RecordingNotifier()
    planner = RaisingClaude(_AUTH_ERR)          # the login stays broken

    await _tick(store, eng, notifier, planner=planner)   # trips auth → pause
    await _tick(store, eng, notifier, planner=planner)   # ping #1
    eng.set_global_pause(_now_ms() - 1000, eng.global_pause()[1])  # probe window
    await _tick(store, eng, notifier, planner=planner)   # re-probe: still broken → re-pause
    await _tick(store, eng, notifier, planner=planner)   # ping #2 (the reminder)

    assert sum("usage limit lifted" in m for m in notifier.sent) == 0  # never a false resume
    assert sum("re-login" in m for m in notifier.sent) == 2            # periodic reminder
    assert eng.global_pause()[0] > _now_ms()                            # paused again


async def test_auth_pause_expiry_after_fix_resumes_work_without_false_ping(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    eng = FlaggedPausableEngine()
    notifier = RecordingNotifier()

    eng.set_global_pause(_now_ms() + 60_000, "auth (goal cognition)")
    await _tick(store, eng, notifier)                    # ping while paused
    eng.set_global_pause(_now_ms() - 1000, "auth (goal cognition)")  # probe window
    fixed = FakeClaude()                                 # re-login happened: calls succeed
    await _tick(store, eng, notifier, planner=fixed)

    assert eng.global_pause()[0] == 0                    # pause cleared
    assert fixed.calls >= 1                              # work actually resumed
    assert sum("usage limit lifted" in m for m in notifier.sent) == 0  # no false resume


async def test_auth_resume_suppression_survives_queue_lazy_clear(tmp_path):
    """Named regression for the invariant-guard find (2026-07-21): the task
    queue's 10s pump lazily clears an expired pause (reason included) long
    before the ~15-min heartbeat looks — the dominant production ordering. The
    "no false 'usage limit lifted' for an auth episode" contract must key on
    the kind persisted with the ping, not the live pause_reason."""
    store = GoalStore(tmp_path, now=Clock())
    eng = FlaggedPausableEngine()
    notifier = RecordingNotifier()

    eng.set_global_pause(_now_ms() + 60_000, "auth: OAuth session expired")
    await _tick(store, eng, notifier)            # auth pause ping (kind persisted)
    eng.clear_global_pause()                     # the queue pump got there first
    await _tick(store, eng, notifier)            # heartbeat sees NO pause at all

    assert sum("usage limit lifted" in m for m in notifier.sent) == 0
    assert eng.pause_notified() is False         # flag still cleared (no leak)


async def test_quota_resume_ping_survives_the_same_lazy_clear(tmp_path):
    """The suppression is auth-only: a genuine quota episode must still
    announce its resume on the pump-cleared ordering."""
    store = GoalStore(tmp_path, now=Clock())
    eng = FlaggedPausableEngine()
    notifier = RecordingNotifier()

    eng.set_global_pause(_now_ms() + 60_000, "quota: weekly cap")
    await _tick(store, eng, notifier)            # quota pause ping
    eng.clear_global_pause()                     # pump cleared it first
    await _tick(store, eng, notifier)

    assert sum("usage limit lifted" in m for m in notifier.sent) == 1
