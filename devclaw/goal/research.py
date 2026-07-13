"""The ``investigating`` lifecycle phase — research before proposing anything.

A senior dev handed an outcome ("make the dashboard usable") checks two things
before writing code: what the repo does *today*, and what *good* looks like for
this kind of thing. This module is the second half — given the objective and a
read-only analysis of the current repo, it synthesizes a **discovery brief**:

  - **Current state** — what the repo does now (grounded in the repo analysis).
  - **Gap to good** — where it falls short of the outcome.
  - **Best-practice checklist** — what a good <thing> covers, so the planner and
    evaluator have a concrete bar to align against.

Cognition only; the read-only repo analysis it consumes is dispatched by the
engine (review_repository) and polled by the tick — this module just turns that
analysis + the objective into the brief. Injected ``ClaudeCaller`` so tests stub
the LLM.
"""

from __future__ import annotations

import asyncio
import os

from .models import Goal
from .planner import ClaudeCaller
from ..task_git import _review_repo_context_sync

#: when on, a newly created goal starts at lifecycle "new" and investigates
#: (read-only repo analysis → discovery brief) before executing. Off → new goals
#: go straight to executing the flat backlog (legacy behavior).
INVESTIGATE_ENABLED = os.environ.get("DEVCLAW_GOAL_INVESTIGATE", "1") not in ("0", "false", "")


class GoalResearchError(RuntimeError):
    """Raised when the discovery synthesis produces no usable brief."""


async def _workspace_snapshot(workspace_dir: str) -> str:
    """Async wrapper — runs the blocking snapshot collector in a thread so it
    never blocks the event loop (same offload rationale as the task queue's
    ``_git_diff``). Looks up :func:`_review_repo_context_sync` as a module
    global so tests can patch it here. Best-effort like the sync body: an
    empty/missing ``workspace_dir`` degrades to '' and it never raises."""
    if not (workspace_dir or "").strip():
        return ""
    return await asyncio.to_thread(_review_repo_context_sync, workspace_dir)


async def discovery_brief(
    goal: Goal, repo_analysis: str, *, caller: ClaudeCaller,
    repo_context: "str | None" = None,
) -> str:
    """Synthesize the discovery brief from the objective + a read-only repo
    analysis. Raises :class:`GoalResearchError` if the model returns nothing
    usable (the caller decides how to degrade — investigation is foundational but
    must not wedge a goal forever).

    ``repo_context`` is a mechanically-collected snapshot of the goal's actual
    workspace (remote/branch/HEAD/key files — :func:`_workspace_snapshot`). It
    anchors the synthesis when the analysis degrades to a failure placeholder:
    host-side ``claude`` inherits devclaw's own cwd, and without grounding it
    fills the gap with the WRONG repo (triage F4 2026-07-13, sibling of the
    #227 wrong-codebase review). Default ``None`` → the section is omitted and
    existing call sites are unaffected."""
    from ..prompts import load_prompt

    done = f"\nDone when: {goal.done_when}" if goal.done_when else ""
    context_block = ""
    if repo_context and repo_context.strip():
        context_block = (
            "\nREPOSITORY CONTEXT (facts collected mechanically from the goal's"
            " workspace — the source of truth for repo identity and which files"
            " exist):\n---\n" + repo_context.strip() + "\n---\n"
        )
    prompt = load_prompt(
        "research-discovery",
        objective=goal.objective,
        done_when=done,
        repo_analysis=repo_analysis or "(no analysis captured)",
        repo_context=context_block,
    )
    brief = (await caller(prompt)).strip()
    if not brief:
        raise GoalResearchError("discovery synthesis returned an empty brief")
    return brief
