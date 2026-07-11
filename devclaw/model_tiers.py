"""Model tiering — the single cognition cost lever.

Three host-side tiers (values for ``claude --model``: an alias like
``sonnet``/``opus`` or a full id) cover every one-shot cognition role.
Which ROLE runs at which TIER is a design decision that lives in the
table below and changes by PR — it is deliberately not per-role
configuration: twelve per-role env vars existed before this module and
none was ever set to a non-default value on any host.

The in-sandbox coding agent's model is separate (``DEVCLAW_EXEC_MODEL``,
a full model id — the token/quota bulk; see ``engine/``).

Empty string → ``None`` → the account's default model.
"""

from __future__ import annotations

import os

MODEL_LIGHT = os.environ.get("DEVCLAW_MODEL_LIGHT", "haiku") or None
MODEL_STANDARD = os.environ.get("DEVCLAW_MODEL_STANDARD", "sonnet") or None
MODEL_DEEP = os.environ.get("DEVCLAW_MODEL_DEEP", "opus") or None

#: role → tier. Deep = rare, high-leverage calls where a wrong answer is
#: expensive to unwind (plans, decompositions, firming). Standard = judgment
#: calls at volume (reviews, evals, grilling). Light = mechanical prose
#: (summaries, failure classification).
_ROLE_TIER: dict[str, str | None] = {
    "planner": MODEL_DEEP,          # plan_goal — one-shot DAG decomposition
    "firming": MODEL_DEEP,          # goal firming — the contract the rest executes
    "decomposer": MODEL_DEEP,       # done_when → checklist
    "world_research": MODEL_DEEP,   # investigation discovery brief
    "goal_planner": MODEL_STANDARD, # next-action pick (bounded JSON, light)
    "goal_eval": MODEL_STANDARD,    # direction evaluator
    "grill": MODEL_STANDARD,        # scope_grill conversation turns
    "review": MODEL_STANDARD,       # pre-PR adversarial review gate
    "trend": MODEL_STANDARD,        # trend-signal summarize/classify
    "judge": MODEL_LIGHT,           # failure-analysis judge
    "summary": MODEL_LIGHT,         # per-delivery plain-prose summary
}


def model_for(role: str) -> str | None:
    """The model a cognition role runs at. Unknown role = a programming
    error — fail loud rather than silently defaulting a new role to the
    wrong cost tier."""
    if role not in _ROLE_TIER:
        raise KeyError(f"unknown cognition role {role!r} — add it to _ROLE_TIER")
    return _ROLE_TIER[role]
