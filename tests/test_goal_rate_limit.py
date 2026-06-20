"""Goal-layer quota pause — a usage limit reaching goal cognition pauses the whole
layer (0 tokens) and auto-resumes, instead of crash-looping + burning quota."""
from __future__ import annotations

import pytest

from devclaw.goal_tick import Outcome, tick_all
from devclaw.goal_store import GoalStore
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
