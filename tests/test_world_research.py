"""World research — mechanism tests for the from-scratch domain-research module.

Cognition-quality grading (does the brief name useful exemplars? does the
defer list shrink scope sensibly?) lives in tests/chain/. Here we test the
mechanism: stub the cognition call, assert wiring is right + the should_fire
rule fires in the right cases.
"""

from __future__ import annotations

import pytest

from devclaw.goal.models import Goal
from devclaw.goal.world_research import (
    WorldResearchError,
    should_fire,
    world_brief,
)


def _goal(repo_url: str | None = None, done_when: str = "") -> Goal:
    return Goal(
        id="g",
        objective="Build a minimal CRM for one user.",
        cadence="1d",
        engine="openhands",
        workspace_dir="/tmp/x",
        repo_url=repo_url,
        verify_cmd=None,
        open_pr=True,
        done_when=done_when,
        backlog=[],
        stub_acceptable=[],
    )


# ---- should_fire decision rule ---------------------------------------------


def test_fires_for_from_scratch_no_repo_url():
    """The load-bearing case: no repo to analyze → world research is the
    only grounding the chain gets."""
    assert should_fire(_goal(repo_url=None)) is True


def test_fires_for_from_scratch_blank_repo_url():
    """A whitespace-only repo_url should be treated as no repo, not as a
    pinned URL the caller meant something by."""
    assert should_fire(_goal(repo_url="   ")) is True


def test_skips_for_existing_repo():
    """Existing-repo goals run the existing repo-research path; firing
    world-research on them would burn quota for no added grounding."""
    assert should_fire(_goal(repo_url="https://github.com/x/y.git")) is False


# ---- world_brief cognition wiring ------------------------------------------


@pytest.mark.asyncio
async def test_brief_passes_objective_and_spec_to_prompt():
    captured = {}

    async def fake_caller(prompt: str) -> str:
        captured["prompt"] = prompt
        return "## Real-world exemplars\n- HubSpot — full-featured CRM."

    goal = _goal(done_when="contacts CRUD works + login")
    out = await world_brief(goal, spec="# spec\nin: contacts", caller=fake_caller)
    assert out.startswith("## Real-world exemplars")
    # The prompt must carry the objective, done_when, and the spec body so
    # the model can ground its exemplars in this specific project.
    assert "Build a minimal CRM" in captured["prompt"]
    assert "contacts CRUD works" in captured["prompt"]
    assert "in: contacts" in captured["prompt"]


@pytest.mark.asyncio
async def test_empty_brief_raises():
    """An empty response is unusable downstream — caller decides degrade."""
    async def fake_caller(prompt: str) -> str:
        return "   \n  "

    with pytest.raises(WorldResearchError):
        await world_brief(_goal(), spec="x", caller=fake_caller)


@pytest.mark.asyncio
async def test_brief_strips_whitespace():
    async def fake_caller(prompt: str) -> str:
        return "\n\n## Real-world exemplars\n- X\n\n"

    out = await world_brief(_goal(), spec="x", caller=fake_caller)
    assert out.startswith("##")
    assert not out.endswith("\n\n")


@pytest.mark.asyncio
async def test_brief_handles_no_spec_gracefully():
    """spec=='' is a valid case (caller skipped the grill); the prompt
    surfaces a placeholder rather than passing the empty string through."""
    captured = {}

    async def fake_caller(prompt: str) -> str:
        captured["prompt"] = prompt
        return "## Real-world exemplars\n- X"

    await world_brief(_goal(), spec="", caller=fake_caller)
    # The placeholder must be in the prompt so the model knows the grill
    # didn't run, rather than seeing literal empty spec context.
    assert "no spec" in captured["prompt"].lower()


@pytest.mark.asyncio
async def test_brief_handles_no_done_when():
    """done_when='' is common pre-firming. The prompt omits the done_when
    line rather than passing an empty value the model has to interpret."""
    captured = {}

    async def fake_caller(prompt: str) -> str:
        captured["prompt"] = prompt
        return "## ok\n- x"

    goal = _goal(done_when="")
    await world_brief(goal, spec="x", caller=fake_caller)
    # No "Done when:" line — we drop the section entirely when empty.
    assert "Done when:" not in captured["prompt"]
