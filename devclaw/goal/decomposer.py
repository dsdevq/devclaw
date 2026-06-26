"""The goal decomposer — the new SECOND HALF of the ``investigating`` phase.

Today's lifecycle::

    Goal created
      ↓ lifecycle: investigating
    dispatch review_repository → repo_analysis (prose)
      ↓
    research.discovery_brief() → prose brief (## Current state / Gap / Good)
      ↓ lifecycle: executing
    per-tick planner reads brief + backlog → emits ONE action

What this module adds (Pillar 1)::

    Goal created
      ↓ lifecycle: investigating
    review_repository → repo_analysis                       [existing]
      ↓
    research.discovery_brief() → prose brief                 [existing]
      ↓
    decompose() → checklist.yaml                            [NEW: this module]
      atomic items with evidence_target + addresses_files +
      depends_on; per-tool, not per-clause; SPECIFIC backing
      service names from the digest.
      ↓ lifecycle: executing
    per-tick planner reads brief + CHECKLIST → emits action
    ADDRESSING pending items (not free-form text). The gate then
    verifies item-by-item, not against done_when prose.

Why this closes the 2026-06-25 finance-sentry-mcp-readonly failure mode:
the agent shipped 16 stubs because the planner was passing prose to it
and the gate was judging prose. With a checklist, the planner dispatches
"wire `GetAccountsTool.Execute` to `IQueryHandler<GetAccountsQuery,
GetAccountsResult>`" + the gate verifies that exact wire — a stub
returning ``not_yet_available`` fails the per-item evidence check.

Same shape as ``planner.py`` / ``evaluator.py``: cognition is ``claude --print``,
JSON/YAML extraction + validation are pure, the caller is injected so
tests stub the LLM without subprocess.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional

from .checklist import ChecklistParseError, parse_checklist
from .models import Checklist, Goal

ClaudeCaller = Callable[[str], Awaitable[str]]

#: the decomposer's model tier. Runs ONCE per goal at lifecycle transition;
#: high-leverage (gets the structured plan right) → defaults to Opus, same as
#: the DAG planner. Override per-goal via env when you want Sonnet's speed.
DECOMPOSER_MODEL = os.environ.get("DEVCLAW_GOAL_DECOMPOSER_MODEL", "opus") or None


class GoalDecomposerError(Exception):
    """Decomposition produced no usable checklist. Carries the raw output on
    ``.raw`` so the caller can log + iterate the prompt."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def build_prompt(
    goal: Goal,
    *,
    discovery_brief: str = "",
    repo_digest: str = "",
) -> str:
    """Compose the full decomposer prompt. The system prompt
    (``prompts/decomposer.md``) is concatenated with the goal's facts +
    prior-pass discovery brief + the curated repo digest. The digest is the
    decomposer's GROUND TRUTH for what services already exist."""
    from ..prompts import load_prompt

    backlog = "\n".join(f"  - {b}" for b in goal.backlog) or "  (none listed)"
    parts = [
        load_prompt("decomposer"),
        "\n## Goal",
        f"objective: {goal.objective}",
        f"done_when: {goal.done_when or '(not specified)'}",
        "backlog:",
        backlog,
    ]
    if discovery_brief:
        parts += [
            "\n## Discovery brief (prior pass — current state · gap · what good looks like)",
            discovery_brief.strip(),
        ]
    if repo_digest:
        parts += [
            "\n## Repo digest (curated read — your GROUND TRUTH for what exists)",
            repo_digest.strip(),
        ]
    parts.append("\nReturn the YAML now.")
    return "\n".join(parts)


async def decompose(
    goal: Goal,
    *,
    claude_caller: ClaudeCaller,
    discovery_brief: str = "",
    repo_digest: str = "",
) -> Checklist:
    """Run the decomposer cognition call → validated :class:`Checklist`.
    ``claude_caller`` is injected so tests stub the LLM. Raises
    :class:`GoalDecomposerError` if the model returns nothing usable or its
    YAML fails the schema contract."""
    prompt = build_prompt(goal, discovery_brief=discovery_brief, repo_digest=repo_digest)
    raw = await claude_caller(prompt)
    try:
        return parse_checklist(raw)
    except ChecklistParseError as exc:
        raise GoalDecomposerError(
            f"decomposer output failed schema validation: {exc}", raw
        ) from exc


def default_caller() -> ClaudeCaller:
    """Production cognition caller bound to the decomposer tier (lazy import
    so tests that inject a fake never touch the subprocess)."""
    from ..planner import claude_with_model

    return claude_with_model(DECOMPOSER_MODEL, role="goal_decomposer")
