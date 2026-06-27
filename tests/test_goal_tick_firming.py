"""End-to-end integration: ``tick_goal`` dispatches to the firming PhaseHandler
when lifecycle is ``firming``, and ``_resolve_discovery`` lands new goals in
firming (not executing) when ``FIRMING_ENABLED`` is set.

Tick-level checks — the handler's own state-machine is covered in
``test_firming_handler.py``."""

from __future__ import annotations

import json

import pytest

from devclaw.goal.firmed import parse_firmed
from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.phases import PhaseResult, registry
from devclaw.goal.phases.firming import FirmingHandler
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


DRAFT_WITH_UNKNOWNS = """\
status: needs_owner_answers
round: 1
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns:
  - id: cf-u1
    question: Period model — calendar month or rolling 30d?
    why: No reporting framework to copy from.
    options: [calendar_month, rolling_30d]
"""

DRAFT_FIRMED = """\
status: firmed
round: 1
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns: []
"""


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


async def _tick(store, goal_id, planner, evaluator, engine, notifier):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="", prepare_ws=fake_prepare, eval_every=99,
    )


@pytest.fixture(autouse=True)
def reset_phase_registry():
    yield
    registry.reset()


@pytest.mark.asyncio
async def test_firming_lifecycle_routes_through_handler(tmp_path):
    """A goal at lifecycle=firming hits the registry; firming returns BLOCKED
    when the draft has unknowns; the planner is NEVER called (cognition is
    owned by the handler, not by tick's executing path)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="firming"))
    firming_caller = FakeClaude(DRAFT_WITH_UNKNOWNS, role="goal_firming")
    registry.register("firming", FirmingHandler(caller=firming_caller))

    planner = FakeClaude(role="goal_planner")
    evaluator = FakeClaude(role="goal_evaluator")
    engine = FakeEngine()
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier)

    assert out is Outcome.BLOCKED
    assert firming_caller.calls == 1
    assert planner.calls == 0  # tick did NOT call the executing-path planner
    assert evaluator.calls == 0
    after = store.load_status("g")
    assert after.lifecycle == "firming"
    assert after.phase == "blocked"


@pytest.mark.asyncio
async def test_firming_clean_round_advances_lifecycle(tmp_path):
    """A clean firming round → ADVANCED, lifecycle=executing — the heartbeat
    immediately re-pokes (covered in service tests) so the executor starts."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="firming"))
    registry.register(
        "firming",
        FirmingHandler(
            caller=FakeClaude(DRAFT_FIRMED, role="goal_firming"),
            decomposer_caller=FakeClaude(role="goal_decomposer"),
        ),
    )

    planner = FakeClaude(role="goal_planner")
    evaluator = FakeClaude(role="goal_evaluator")
    out = await _tick(
        store, "g", planner, evaluator, FakeEngine(), RecordingNotifier(),
    )

    assert out is Outcome.ADVANCED
    assert store.load_status("g").lifecycle == "executing"
    assert store.read_firmed_draft("g") is not None


@pytest.mark.asyncio
async def test_blocked_firming_goal_does_not_re_fire_handler(tmp_path):
    """Firming is event-driven — once blocked on answers, ticks must NOT call
    the firming cognition again (the owner triggers the next round via
    answer_unknowns, not the heartbeat)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_firmed_draft("g", parse_firmed(DRAFT_WITH_UNKNOWNS))
    store.save_status(
        "g", GoalStatus(lifecycle="firming", phase="blocked", blocked_on="1 question"),
    )
    firming_caller = FakeClaude(DRAFT_FIRMED, role="goal_firming")
    registry.register("firming", FirmingHandler(caller=firming_caller))

    out = await _tick(
        store, "g", FakeClaude(role="goal_planner"), FakeClaude(role="goal_evaluator"),
        FakeEngine(), RecordingNotifier(),
    )

    assert out is Outcome.IDLE
    assert firming_caller.calls == 0  # the quota guardrail: 0 tokens while blocked


@pytest.mark.asyncio
async def test_discovery_lands_in_firming_when_enabled(tmp_path, monkeypatch):
    """When DEVCLAW_GOAL_FIRMING=1, the discovery-resolve hook drops the goal
    into lifecycle=firming, NOT lifecycle=executing — the firming handler then
    fires on the very next tick (covered above)."""
    import devclaw.goal.tick as tick_mod
    from devclaw.goal.phases import firming as firming_mod

    monkeypatch.setattr(firming_mod, "FIRMING_ENABLED", True)

    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    # discovery just finished — in_flight is the discovery ref
    store.save_status(
        "g",
        GoalStatus(
            lifecycle="investigating", phase="in_flight",
            in_flight=InFlight(
                "devclaw", "review_repository", "t-disc", "task",
                "analysis brief", is_discovery=True,
            ),
        ),
    )
    # discovery synthesis caller (reused for evaluator_caller in tick wiring)
    evaluator = FakeClaude(
        "## Current state\nbare.\n## Gap to good\nno report.\n## What good looks like\n- monthly report",
        role="goal_evaluator",
    )
    planner = FakeClaude(role="goal_planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="the repo has X services and Y",
    ))

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator,
        notifier=RecordingNotifier(), notify_url="", prepare_ws=fake_prepare,
        eval_every=99,
    )

    assert out is Outcome.ADVANCED
    after = store.load_status("g")
    assert after.lifecycle == "firming"
    assert after.phase == "idle"
