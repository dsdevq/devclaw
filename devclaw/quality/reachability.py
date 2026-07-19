"""Browser-gate reachability judge — the grounded, fail-closed escape valve for
the browser-E2E gate's one known false positive.

The gate (``devclaw.quality.browser_gate``) fires MECHANICALLY on any frontend
file, on purpose: a change that "didn't declare it needs a browser" must still be
caught. That bluntness has one legitimate false positive — a UI change that is not
rendered anywhere in the RUNNING app has nothing for Playwright to exercise (a
design-system component no route imports yet; the finance-sentry ``cmn-tab-group``
that wedged for ~14h, 2026-07-17). Its real proof is its unit test + story, not a
full-app browser run.

Rather than a static "library-only" path allowlist (which rots and is
project-specific), this module answers the GENERIC question: *is the changed UI
reachable in the running application?* — reasoned per-change, grounded ONLY in the
task workspace (the #227 discipline), and biased hard toward "require the run":

- The judgment is INDEPENDENT of the agent that wrote the diff (consulted by the
  settle path, never self-declared) — the gate exists precisely because an
  implementer's own green tests can't be trusted to say "no browser needed".
- It can only ever DOWNGRADE a block to a ship, and only on an affirmative
  ``reachable == "no"``. ``yes`` / ``unknown`` / unparseable / any exception all
  leave the block standing. So it is strictly safe: worst case it is a no-op and
  the gate behaves exactly as before.

Same shape as ``review_diff`` — cognition is host-side ``claude`` (OAuth, no API
key), tiered via the ``reachability`` role; prompt-building + validation are pure,
so this is unit-testable with a stubbed caller.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from ..model_tiers import model_for as _model_for
from ..llm_call import PlannerError, claude_with_model, extract_json

import json

#: Bounded judgment over a diff + repo context — Standard tier, like the review
#: gate. Empty → account default.
REACHABILITY_MODEL = _model_for("reachability")
#: Same generous budget as the review gate — it reads a diff + repo snapshot and
#: reasons over both.
REACHABILITY_TIMEOUT_MS = 180_000
#: default cognition caller, bound to the reachability tier + timeout
reachability_caller = claude_with_model(
    REACHABILITY_MODEL, role="reachability", timeout_ms=REACHABILITY_TIMEOUT_MS
)

#: cap the diff we send — a huge change can't blow the prompt / quota. Head-kept
#: (the start, where the substantive files usually are), truncation noted.
_MAX_DIFF_CHARS = 60_000

_VALID = ("yes", "no", "unknown")


def _clip(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return diff[:_MAX_DIFF_CHARS] + "\n…[diff truncated for length]…\n"


def build_reachability_prompt(*, diff: str, repo_context: Optional[str] = None) -> str:
    """Assemble the reachability prompt: the base contract + a grounded
    REPOSITORY CONTEXT block (the #227 shape — repo facts are the ONLY basis, and
    the contract already says 'absent ⇒ unknown') + the diff. Pure."""
    from .prompts import load_prompt

    parts = [load_prompt("browser-reachability")]
    if repo_context and repo_context.strip():
        parts.append(
            "REPOSITORY CONTEXT (facts from the task workspace — the ONLY source of "
            "truth for routes, module imports, and which files/dirs exist):\n"
            + repo_context.strip()
        )
    parts.append(f"DIFF UNDER REVIEW:\n{_clip(diff)}")
    return "\n\n".join(parts)


def validate_reachability(parsed: object) -> dict:
    """Normalize the model's answer into ``{reachable, rationale}``. An
    out-of-range or missing ``reachable`` coerces to ``"unknown"`` — the SAFE
    default (unknown never downgrades the block). Only a non-JSON response
    reaches the caller as a raise (via :func:`judge_reachability`)."""
    if not isinstance(parsed, dict):
        return {"reachable": "unknown", "rationale": "non-object response"}
    val = parsed.get("reachable")
    reachable = val if val in _VALID else "unknown"
    rationale = parsed.get("rationale")
    rationale = rationale.strip() if isinstance(rationale, str) else ""
    return {"reachable": reachable, "rationale": rationale}


async def judge_reachability(
    *,
    diff: str,
    repo_context: Optional[str] = None,
    claude_caller: Callable[[str], Awaitable[str]] = reachability_caller,
) -> dict:
    """Judge whether the UI a diff changes is reachable in the running app.
    Returns ``{reachable: yes|no|unknown, rationale}``. ``claude_caller`` is
    injected so tests stub the subprocess. Raises PlannerError only on an
    unparseable response — the settle caller treats any raise as fail-closed
    (block stands)."""
    prompt = build_reachability_prompt(diff=diff, repo_context=repo_context)
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Reachability JSON parse failed: {err}", raw) from err
    return validate_reachability(parsed)
