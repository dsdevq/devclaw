"""Pre-PR adversarial diff-review gate — make "green" mean "trustworthy".

The verify gate proves a change *behaves* (tests pass) and the test-integrity
guard proves it didn't go green by gutting the suite — but neither one *reads the
code*. That's the hole a spectator-PO can't cover: a `.Take(0)` dead-code line, a
happy-path-only implementation, logic stuffed in the wrong layer, an untested
frontend change all sail through a green gate. This module closes it: after the
gate passes but BEFORE the PR opens, a separate Claude pass reviews the diff
against the ticket and the production quality bar, and returns a structured
verdict. On `request_changes` the task queue feeds the issues back into the
existing retry loop exactly like a gate failure, escalating to the owner after N.

Same shape as the eval judge / planner: cognition is `claude` (host-side, OAuth,
no API key), tiered via DEVCLAW_REVIEW_MODEL; the prompt-building and response
validation are pure, so this is unit-testable with a stubbed caller.
"""

from __future__ import annotations

import json
import os
from typing import Awaitable, Callable, Optional

from ..planner import PlannerError, claude_with_model, extract_json

#: Adversarial code review is judgment-heavy — Sonnet is the right tier (matches
#: the scope grill; heavier than the Haiku classification judge, lighter than the
#: Opus planner). Empty → account default.
from ..model_tiers import model_for as _model_for
REVIEW_MODEL = _model_for("review")
#: per-call timeout. The review reads a diff up to _MAX_DIFF_CHARS (60 KB) and
#: reasons over the whole thing on Sonnet — it was the one large-input cognition
#: role still on the global 90s ceiling, so a big diff timed out, failed the gate
#: closed, burned the retry budget, and escalated to the owner. Match the other
#: large-output sonnet role (grill = 180s) rather than the 90s PLANNER_TIMEOUT_MS.
REVIEW_TIMEOUT_MS = 180_000
#: default cognition caller for the review, bound to the review tier + timeout
review_caller = claude_with_model(REVIEW_MODEL, role="review", timeout_ms=REVIEW_TIMEOUT_MS)

#: cap the diff we send so a huge change can't blow the prompt / quota. Tail-kept
#: would lose the header, so we head-keep (the start of the diff, where the
#: substantive files usually are) and note the truncation.
_MAX_DIFF_CHARS = 60_000

_SEVERITIES = ("blocker", "major", "minor")

def _clip_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return (
        diff[:_MAX_DIFF_CHARS]
        + f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} chars; review what is shown ...]"
    )


def build_review_prompt(*, goal: str, kind: str, diff: str) -> str:
    from ..prompts import load_prompt

    return "\n\n".join(
        [
            load_prompt("review-gate"),
            f"TICKET ({kind}):\n{goal}",
            f"DIFF UNDER REVIEW:\n{_clip_diff(diff)}",
        ]
    )


def validate_review(parsed: object) -> dict:
    """Validate + normalize the model's review into a verdict dict. Enforces the
    invariant that request_changes ⇔ there is a blocker/major issue, so the
    verdict can't disagree with its own issue list."""
    if not isinstance(parsed, dict):
        raise PlannerError("Review response must be a JSON object")
    verdict = parsed.get("verdict")
    if verdict not in ("approve", "request_changes"):
        raise PlannerError(
            f"Review verdict must be 'approve' or 'request_changes', got {verdict!r}"
        )
    summary = parsed.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    raw_issues = parsed.get("issues")
    issues: list[dict] = []
    if isinstance(raw_issues, list):
        for it in raw_issues:
            if not isinstance(it, dict):
                continue
            sev = it.get("severity")
            sev = sev if sev in _SEVERITIES else "minor"
            issues.append(
                {
                    "severity": sev,
                    "location": str(it.get("location", "")).strip(),
                    "problem": str(it.get("problem", "")).strip(),
                    "fix": str(it.get("fix", "")).strip(),
                }
            )
    blocking = [i for i in issues if i["severity"] in ("blocker", "major")]
    # Reconcile verdict with the issue list — the issues are the evidence, so they
    # win: a "request_changes" with no blocking issue is downgraded; an "approve"
    # that nonetheless lists a blocker/major is upgraded to request_changes.
    final_verdict = "request_changes" if blocking else "approve"
    return {
        "verdict": final_verdict,
        "summary": summary,
        "issues": issues,
        "blocking": blocking,
    }


def format_feedback(review: dict) -> str:
    """Render a request_changes verdict as actionable feedback fed back into the
    retry loop (becomes the task's failure context, like a gate failure)."""
    lines = ["code review requested changes before this can ship:"]
    if review.get("summary"):
        lines.append(review["summary"])
    for i in review.get("blocking", []):
        loc = f" [{i['location']}]" if i.get("location") else ""
        fix = f" — fix: {i['fix']}" if i.get("fix") else ""
        lines.append(f"- ({i['severity']}){loc} {i['problem']}{fix}")
    lines.append(
        "Address every blocker/major issue above (do not weaken tests to do it), "
        "then re-verify."
    )
    return "\n".join(lines)


async def review_diff(
    *,
    goal: str,
    kind: str,
    diff: str,
    claude_caller: Callable[[str], Awaitable[str]] = review_caller,
) -> dict:
    """Review one diff into a validated verdict dict. ``claude_caller`` is injected
    so tests can stub the subprocess. Raises PlannerError if the model returns
    unparseable/invalid JSON (the caller decides whether to fail open)."""
    prompt = build_review_prompt(goal=goal, kind=kind, diff=diff)
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Review JSON parse failed: {err}", raw) from err
    return validate_review(parsed)
