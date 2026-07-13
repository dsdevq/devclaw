"""The discovery synthesis turns the objective + a read-only repo analysis into a
discovery brief. It must surface an empty/unusable result as an error (the tick
degrades gracefully on that), and feed both the objective and the analysis to the
model."""

from __future__ import annotations

import pytest

from devclaw.goal.models import Goal
from devclaw.goal.research import GoalResearchError, discovery_brief


def _goal(**kw) -> Goal:
    base = dict(id="g", objective="make the dashboard usable", cadence="1d",
                engine="devclaw", workspace_dir="/repo", done_when="a non-tech user can do the core task")
    base.update(kw)
    return Goal(**base)


@pytest.mark.asyncio
async def test_synthesizes_brief_from_objective_and_analysis():
    seen = {}

    async def caller(prompt: str) -> str:
        seen["prompt"] = prompt
        return "## Current state\nA bare API.\n## Gap to good\nNo UI.\n## What good looks like\n- usable pages"

    out = await discovery_brief(_goal(), "the repo has 3 endpoints and no frontend", caller=caller)
    assert "Current state" in out
    assert "make the dashboard usable" in seen["prompt"]          # objective fed in
    assert "3 endpoints" in seen["prompt"]                        # repo analysis fed in
    assert "non-tech user" in seen["prompt"]                      # done_when fed in


@pytest.mark.asyncio
async def test_empty_brief_raises():
    async def caller(prompt: str) -> str:
        return "   "

    with pytest.raises(GoalResearchError):
        await discovery_brief(_goal(), "analysis", caller=caller)


@pytest.mark.asyncio
async def test_repo_context_block_included_when_provided():
    """A mechanically-collected workspace snapshot rides into the prompt as a
    REPOSITORY CONTEXT section — the grounding channel for a failed/empty
    analysis (triage F4 GAP A, 2026-07-13)."""
    seen = {}

    async def caller(prompt: str) -> str:
        seen["prompt"] = prompt
        return "## Current state\nok"

    await discovery_brief(
        _goal(), "review failed (no analysis captured)", caller=caller,
        repo_context="git_remote_origin: https://example.com/x.git\nglobal.json: file",
    )
    assert "REPOSITORY CONTEXT (facts collected mechanically" in seen["prompt"]
    assert "global.json: file" in seen["prompt"]
    assert "https://example.com/x.git" in seen["prompt"]


@pytest.mark.asyncio
async def test_repo_context_section_omitted_when_absent():
    """Default None → no REPOSITORY CONTEXT section header (existing call
    sites unaffected); the honesty rules are part of the template regardless."""
    seen = {}

    async def caller(prompt: str) -> str:
        seen["prompt"] = prompt
        return "## Current state\nok"

    await discovery_brief(_goal(), "analysis", caller=caller)
    assert "REPOSITORY CONTEXT (facts collected mechanically" not in seen["prompt"]
    # the anti-inference rules hold even without a snapshot:
    assert "missing or failed" in seen["prompt"]
    assert "working directory" in seen["prompt"]
