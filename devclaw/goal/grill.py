"""The ``grilling`` lifecycle phase — align on scope before building.

Reuses the one-question-at-a-time elicitation grill (``elicitation.next_step``),
but wired in front of a DURABLE goal instead of a one-shot project: questions go
out over Telegram and answers come back asynchronously across heartbeats, so the
loop is durable (transcript on disk) and quota-safe (a goal waiting for a reply
spends zero tokens — the tick only runs cognition when a fresh answer is in or
the grill has just begun).

The grill is primed with the discovery brief, so it asks about real gaps, not
things it could have read from the repo. Its output is a **spec** (what to build,
what's out, constraints) — the contract the planner later decomposes.
"""

from __future__ import annotations

import os

from ..elicitation import next_step  # noqa: F401 — re-exported for the tick
from .models import Goal
from .planner import ClaudeCaller

#: off by default — the grill needs the Telegram answer channel wired and a live
#: round-trip validated before it should drive real goals. When off, a goal flows
#: investigating → executing (no grill).
GRILL_ENABLED = os.environ.get("DEVCLAW_GOAL_GRILL", "0") not in ("0", "false", "")

#: model tier for the grill cognition
GRILL_MODEL = os.environ.get("DEVCLAW_GOAL_GRILL_MODEL", "sonnet") or None


def grill_idea(goal: Goal, discovery: str) -> str:
    """Compose the 'idea' the grill reasons over: the owner's outcome + the
    discovery brief, so questions target the real gaps."""
    parts = [f"Outcome the owner wants: {goal.objective}"]
    if goal.done_when:
        parts.append(f"Done when: {goal.done_when}")
    if discovery:
        parts.append(f"\nWhat we already learned investigating the repo:\n{discovery}")
    return "\n".join(parts)


def default_caller() -> ClaudeCaller:
    from ..planner import claude_with_model

    return claude_with_model(GRILL_MODEL)
