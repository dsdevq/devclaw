"""From-scratch goals fire world-research at investigation open-time.

The investigating phase has two branches: world-research when the goal is
from-scratch (no ``repo_url``), repo-research otherwise. These tests cover
the from-scratch branch end-to-end through ``tick_goal``.

Cognition-quality grading of the brief itself lives in tests/chain/.
"""

from __future__ import annotations

import pytest

from devclaw.goal.models import GoalStatus
from devclaw.goal.tick import Outcome, tick_goal
from tests.goal_fakes import (
    Clock,
    FakeClaude,
    FakeEngine,
    RecordingNotifier,
    fake_prepare,
    seed_goal,
)
from devclaw.goal.store import GoalStore


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


async def _tick(store, goal_id, planner, evaluator, engine, notifier, *, world_research=None):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=evaluator, notifier=notifier,
        notify_url="", prepare_ws=fake_prepare, eval_every=99,
        world_research_caller=world_research,
    )


@pytest.mark.asyncio
async def test_from_scratch_goal_fires_world_research(tmp_path):
    """No ``repo_url`` → investigation runs world-research synchronously,
    writes the brief to discovery.md, and transitions to executing."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", repo_url=None)
    store.save_status("g", GoalStatus(lifecycle="investigating"))

    world = FakeClaude(
        response=(
            "## Real-world exemplars\n- HubSpot — full CRM\n\n"
            "## What good MVP looks like\n- contact CRUD\n\n"
            "## Deliberately defer\n- Not in MVP: pipeline\n"
        ),
        role="world_research",
    )
    planner, evaluator, engine, notifier = FakeClaude(), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, world_research=world)

    # Synchronous: no engine dispatch happened.
    assert out is Outcome.ADVANCED
    assert engine.dispatched == []
    # Brief is on disk where the rest of the chain expects it.
    discovery = (tmp_path / "g" / "discovery.md").read_text()
    assert "Real-world exemplars" in discovery
    assert "HubSpot" in discovery
    assert "Deliberately defer" in discovery
    # Lifecycle advanced.
    s = store.load_status("g")
    assert s.lifecycle == "executing"
    assert s.phase == "idle"
    # World-research call counted; the planner & evaluator stayed at 0.
    assert world.calls == 1
    assert planner.calls == 0
    assert evaluator.calls == 0


@pytest.mark.asyncio
async def test_from_scratch_world_research_failure_proceeds_without_brief(tmp_path):
    """If the world-research caller raises, the goal continues without a
    brief rather than wedging — investigation is an enhancement, not a gate."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", repo_url=None)
    store.save_status("g", GoalStatus(lifecycle="investigating"))

    class _Boom:
        calls = 0

        async def __call__(self, prompt: str) -> str:
            type(self).calls += 1
            raise RuntimeError("cognition exploded")

    world = _Boom()
    planner, evaluator, engine, notifier = FakeClaude(), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, world_research=world)

    assert out is Outcome.ADVANCED
    # No brief was written.
    assert not (tmp_path / "g" / "discovery.md").exists()
    # But the goal advanced — failure is non-fatal.
    assert store.load_status("g").lifecycle == "executing"
    # The failure landed in the log.
    log = (tmp_path / "g" / "log.md").read_text()
    assert "world-research failed" in log


@pytest.mark.asyncio
async def test_existing_repo_goal_takes_repo_research_path(tmp_path):
    """``repo_url`` set → world-research does NOT fire; existing repo-research
    dispatch happens (today's behavior). World-research caller MUST NOT be
    invoked."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", repo_url="https://example.com/existing.git")
    store.save_status("g", GoalStatus(lifecycle="investigating"))

    world = FakeClaude(response="SHOULD NOT FIRE", role="world_research")
    planner, evaluator, engine, notifier = FakeClaude(), FakeClaude(), FakeEngine(), RecordingNotifier()

    out = await _tick(store, "g", planner, evaluator, engine, notifier, world_research=world)

    # Today's repo-research path: dispatches review_repository.
    assert out is Outcome.DISPATCHED
    assert len(engine.dispatched) == 1
    assert engine.dispatched[0][0].tool == "review_repository"
    # World-research was not touched.
    assert world.calls == 0
    # No discovery brief yet — that lands when the analysis settles.
    assert not (tmp_path / "g" / "discovery.md").exists()


@pytest.mark.asyncio
async def test_world_research_brief_reads_spec_from_store(tmp_path):
    """The spec the waiter wrote via ``write_spec`` must reach the
    world-research prompt — that's the grounding for the brief."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", repo_url=None)
    store.save_status("g", GoalStatus(lifecycle="investigating"))
    store.write_spec("g", "# spec\n## Scope\nin: contacts, notes\nout: deals\n")

    world = FakeClaude(response="## Real-world exemplars\n- HubSpot\n", role="world_research")
    planner, evaluator, engine, notifier = FakeClaude(), FakeClaude(), FakeEngine(), RecordingNotifier()

    await _tick(store, "g", planner, evaluator, engine, notifier, world_research=world)

    assert "in: contacts, notes" in world.last_prompt
    assert "out: deals" in world.last_prompt
