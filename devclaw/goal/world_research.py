"""World research — what good looks like for this kind of thing, from the
model's training knowledge of real software.

Mirror module to :mod:`devclaw.goal.research` (which does REPO research on an
existing codebase). When a goal is **from-scratch** — no ``repo_url``, the
workspace will be built — there's no repository to analyze. The chain test
surfaced this as the load-bearing missing link: the chef would happily
decompose "build a CRM" with no grounding in what an actual MVP CRM is.

This module fills that gap with a one-shot cognition call. Given the
objective + the agreed spec, the model produces a brief naming real-world
exemplars, distilling what a competent MVP includes, and EXPLICITLY listing
what to defer (the senior product move that prevents scope creep to match
the exemplars).

No web access required for v1 — the frontier model has strong knowledge of
the major SaaS / dev-tool / consumer-app categories. A web-search variant
is a possible follow-up if the deferred deliberately-defer list starts
looking shallow on niche categories; for now, training knowledge is enough
and the brief is owner-reviewable.

Lifecycle wiring is DEFERRED to a follow-up. v1 makes the module available
for direct call (the chain test exercises it); a future PR adds a
``world_researching`` lifecycle phase that fires for from-scratch goals
before the decomposer runs.

See ~/memory/projects/devclaw/chain-map-2026-06-30.md row 9.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from .models import Goal

ClaudeCaller = Callable[[str], Awaitable[str]]

#: the world-research model tier. Runs ONCE per from-scratch goal at
#: alignment time; high-leverage (sets the bar the rest of the chain plans
#: against) → runs at the deep tier.
from ..model_tiers import model_for as _model_for
WORLD_RESEARCH_MODEL = _model_for("world_research")

#: per-call timeout. The brief is ~300-500 words of structured markdown,
#: well within opus's normal generation budget but with the same kind of
#: variance the decomposer showed — set generously and bound by the
#: per-role timeout pattern from planner.py.
WORLD_RESEARCH_TIMEOUT_MS = 180_000


class WorldResearchError(RuntimeError):
    """Raised when world-research returns an empty brief. The caller decides
    how to degrade — world research is foundational for from-scratch but
    must not wedge a goal forever."""


def should_fire(goal: Goal) -> bool:
    """Decision rule for whether to fire world-research on this goal.

    v1 rule: from-scratch only. A goal with a ``repo_url`` (or any
    pre-existing workspace) has an actual codebase to analyze and runs the
    existing repo-research path instead.

    The decision lives here (not in the caller) so the lifecycle layer can
    grow more conditions over time without spreading rules across callers
    — e.g. a future "fire on existing-repo when the planner's first action
    introduces a category the repo doesn't already have" rule lives here.
    """
    return goal.repo_url is None or not goal.repo_url.strip()


async def world_brief(goal: Goal, spec: str, *, caller: ClaudeCaller) -> str:
    """Synthesize the world-research brief from the objective + the agreed
    spec. Raises :class:`WorldResearchError` if the model returns nothing
    usable. ``caller`` is injected so tests stub the LLM."""
    from ..prompts import load_prompt

    done = f"\nDone when: {goal.done_when}" if goal.done_when else ""
    prompt = load_prompt(
        "world-research",
        objective=goal.objective,
        done_when=done,
        spec=spec or "(no spec — caller skipped scope_grill)",
    )
    brief = (await caller(prompt)).strip()
    if not brief:
        raise WorldResearchError("world-research returned an empty brief")
    return brief


def default_caller() -> ClaudeCaller:
    """Production cognition caller bound to the world-research tier. Lazy
    import keeps tests that inject a fake from touching the subprocess."""
    from ..planner import claude_with_model

    return claude_with_model(
        WORLD_RESEARCH_MODEL,
        role="world_research",
        timeout_ms=WORLD_RESEARCH_TIMEOUT_MS,
    )
