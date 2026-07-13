"""Decomposer prompt-build + cognition wrapper. Pure tests with a stubbed
caller — no claude subprocess invoked. The output's QUALITY is validated
separately by the experiment harness (see
~/memory/projects/devclaw/experiments/2026-06-26-decomposer/), not here."""

from __future__ import annotations

import pytest

from devclaw.goal.decomposer import (
    GoalDecomposerError,
    build_prompt,
    decompose,
)
from devclaw.goal.models import Goal


def _goal() -> Goal:
    return Goal(
        id="g",
        objective="Build the finance-sentry read-only MCP for Ledger.",
        cadence="1d",
        engine="devclaw",
        workspace_dir="/ws",
        done_when=(
            "The finance-sentry MCP server exposes the agreed read-only "
            "Ledger surface with finance-sentry-native tool names, direct "
            "reads for authoritative backend data, explicit "
            "not_yet_available stubs for unsupported capabilities, no "
            "mutation or provider-control tools, passing contract tests for "
            "tool names/read-only flags/parity behavior, updated "
            "docs/configuration, and a green backend verification run."
        ),
        backlog=[
            "Audit the existing FinanceSentry.Mcp scaffold.",
            "Implement the read-only MCP tool set.",
        ],
    )


# ---- build_prompt -----------------------------------------------------------


def test_build_prompt_carries_goal_facts():
    prompt = build_prompt(_goal())
    assert "objective:" in prompt
    assert "Build the finance-sentry read-only MCP" in prompt
    assert "done_when:" in prompt
    assert "direct reads for authoritative backend data" in prompt
    assert "backlog:" in prompt


def test_build_prompt_carries_system_directive():
    prompt = build_prompt(_goal())
    # the PROCEDURE step the prompt is load-bearing on
    assert "DECOMPOSE" in prompt
    assert "atomic clauses" in prompt
    assert "evidence_target" in prompt
    # the anti-pattern callout
    assert "Vague items" in prompt or "vague items" in prompt.lower()


def test_build_prompt_sequences_wide_refactors_expand_contract():
    """Wide refactors must decompose as expand -> migrate batches -> contract
    (three depends_on tiers), never as one unfinishable item or as parallel
    items whose addresses_files collide."""
    prompt = build_prompt(_goal())
    assert "EXPAND–CONTRACT" in prompt
    assert "blast radius" in prompt
    assert "expand" in prompt and "contract" in prompt
    assert "One-item wide refactors" in prompt  # the anti-pattern callout
    # the prefactor framing that motivates step 5
    assert "Make the change easy, then make the easy change" in prompt


def test_build_prompt_includes_brief_and_digest_when_present():
    prompt = build_prompt(
        _goal(),
        discovery_brief="## Current state\nThe repo has X.\n",
        repo_digest="Module Foo exposes GetFooQuery.",
        repo_context="git_remote_origin: https://github.com/x/y.git\nglobal.json: file",
    )
    assert "Discovery brief" in prompt
    assert "The repo has X." in prompt
    assert "Repo digest" in prompt
    assert "Module Foo exposes GetFooQuery." in prompt
    # the mechanical snapshot renders as its own section (heading match — the
    # prompt's grounding rule also SAYS "REPOSITORY CONTEXT", so match the
    # injected heading, not the phrase)
    assert "## REPOSITORY CONTEXT (mechanical" in prompt
    assert "https://github.com/x/y.git" in prompt
    assert "global.json: file" in prompt


def test_build_prompt_omits_brief_section_when_empty():
    prompt = build_prompt(_goal())
    assert "Discovery brief" not in prompt
    assert "Repo digest" not in prompt
    # no injected section when context is absent or blank — older call sites
    # byte-unaffected
    assert "## REPOSITORY CONTEXT (mechanical" not in prompt
    blank = build_prompt(_goal(), repo_context="   ")
    assert "## REPOSITORY CONTEXT (mechanical" not in blank


def test_decomposer_prompt_carries_no_inventing_rule():
    """The grounding clauses render: with an absent/thin digest the model must
    cite only REPOSITORY CONTEXT paths and raise open_questions instead of
    inventing paths/symbols/stack; `scaffold: true` is only valid when the
    digest/context shows the scaffold does not already exist (a hallucinated
    scaffold tag strips the adversarial review gate, #225, off its diff)."""
    prompt = build_prompt(_goal())
    assert "Ground every repo fact in what you are given" in prompt
    assert "raise `open_questions` instead of inventing" in prompt
    assert "NEVER infer the stack from the host process" in prompt
    assert "the scaffold does NOT already" in prompt


def test_build_prompt_handles_empty_backlog_without_crashing():
    g = Goal(
        id="g", objective="o", cadence="1d", engine="devclaw",
        workspace_dir="/ws", done_when="d", backlog=[],
    )
    prompt = build_prompt(g)
    assert "(none listed)" in prompt


# ---- decompose (stubbed caller) --------------------------------------------


_VALID_YAML = """\
checklist:
  - id: scaffold
    requirement: Create the FinanceSentry.Mcp.csproj.
    evidence_target: backend/src/FinanceSentry.Mcp/FinanceSentry.Mcp.csproj
    addresses_files: [backend/src/FinanceSentry.Mcp/FinanceSentry.Mcp.csproj]
    depends_on: []
    status: not_started
    evidence: null
  - id: wire-accounts
    requirement: Wire the accounts tool to GetAccountsQuery.
    evidence_target: backend/src/FinanceSentry.Mcp/Tools/AccountsTool.cs — IQueryHandler<GetAccountsQuery,GetAccountsResult>
    addresses_files: [backend/src/FinanceSentry.Mcp/Tools/AccountsTool.cs]
    depends_on: [scaffold]
    status: not_started
    evidence: null
open_questions: []
notes: []
"""


@pytest.mark.asyncio
async def test_decompose_happy_path():
    calls = {"n": 0, "last_prompt": ""}

    async def caller(prompt: str) -> str:
        calls["n"] += 1
        calls["last_prompt"] = prompt
        return _VALID_YAML

    cl = await decompose(_goal(), claude_caller=caller)
    assert calls["n"] == 1
    assert "DECOMPOSE" in calls["last_prompt"]
    assert [i.id for i in cl.items] == ["scaffold", "wire-accounts"]


@pytest.mark.asyncio
async def test_decompose_tolerates_preamble_and_fence():
    async def caller(prompt: str) -> str:
        return (
            "Producing the YAML now.\n\n"
            "```yaml\n" + _VALID_YAML + "```\n"
        )

    cl = await decompose(_goal(), claude_caller=caller)
    assert [i.id for i in cl.items] == ["scaffold", "wire-accounts"]


@pytest.mark.asyncio
async def test_decompose_invalid_yaml_raises_with_raw():
    async def caller(prompt: str) -> str:
        return "checklist:\n  - id: [garbage\n"

    with pytest.raises(GoalDecomposerError) as excinfo:
        await decompose(_goal(), claude_caller=caller)
    assert excinfo.value.raw is not None
    assert "garbage" in excinfo.value.raw


@pytest.mark.asyncio
async def test_decompose_empty_output_raises():
    async def caller(prompt: str) -> str:
        return ""

    with pytest.raises(GoalDecomposerError):
        await decompose(_goal(), claude_caller=caller)


@pytest.mark.asyncio
async def test_decompose_passes_brief_and_digest_through():
    seen = {"prompt": ""}

    async def caller(prompt: str) -> str:
        seen["prompt"] = prompt
        return _VALID_YAML

    await decompose(
        _goal(),
        claude_caller=caller,
        discovery_brief="## Current state\nThe repo has X.\n",
        repo_digest="Module Foo exposes GetFooQuery.",
    )
    assert "The repo has X." in seen["prompt"]
    assert "Module Foo exposes GetFooQuery." in seen["prompt"]
