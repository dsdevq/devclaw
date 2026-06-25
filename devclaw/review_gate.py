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

from .planner import PlannerError, claude_with_model, extract_json

#: Adversarial code review is judgment-heavy — Sonnet is the right tier (matches
#: the scope grill; heavier than the Haiku classification judge, lighter than the
#: Opus planner). Empty → account default.
REVIEW_MODEL = os.environ.get("DEVCLAW_REVIEW_MODEL", "sonnet") or None
#: default cognition caller for the review, bound to the review tier
review_caller = claude_with_model(REVIEW_MODEL, role="review")

#: cap the diff we send so a huge change can't blow the prompt / quota. Tail-kept
#: would lose the header, so we head-keep (the start of the diff, where the
#: substantive files usually are) and note the truncation.
_MAX_DIFF_CHARS = int(os.environ.get("DEVCLAW_REVIEW_MAX_DIFF_CHARS", "60000"))

_SEVERITIES = ("blocker", "major", "minor")

# The quality bar the review enforces. Deliberately mirrors the _QUALITY_BAR the
# engineer is briefed with (openhands-runner/runner.py) — the PM reviews against
# the same contract it set — plus the concrete failure classes dogfooding has
# actually surfaced, so the reviewer hunts for real, named defects rather than
# vague "could be better" notes.
_REVIEW_RULES = """You are DevClaw's senior code reviewer. An autonomous coding
agent was given a ticket and produced a change whose test/build gate already
PASSED. Your job is the part the gate cannot do: read the diff as a demanding
senior engineer and decide whether you would approve this pull request.

A passing gate is necessary but NOT sufficient. Review adversarially — actively
hunt for real defects in the diff against the ticket and this quality bar:

- Dead / no-op / placeholder code: lines that do nothing, can never run, or only
  appear to do work (e.g. an accessibility check that enumerates nothing and so
  can never throw). Every line must do real work.
- Wrong layer / structure: business logic inlined where it doesn't belong instead
  of the relevant service/module; not matching the surrounding architecture.
- Happy-path only: real edge and error cases unhandled (bad/missing input,
  not-found, empty collections, invalid dates, concurrency) when the ticket or
  the code clearly implies them.
- Weak or theatrical tests: tests that assert almost nothing, never exercise the
  failure/edge cases, are tautological, or were weakened/skipped to pass.
- Uncovered change: substantive behaviour the gate does not actually exercise
  (e.g. a frontend/UI change when the gate is a backend test suite) and that the
  diff itself does not verify — call this out explicitly; the green gate is
  misleading here.
- Correctness bugs, security issues, and ignored ticket requirements.
- Style/naming/error-handling that diverges from the existing code.

Be specific and honest, and cite file + location for every issue. Do NOT invent
problems to look thorough: if the change is genuinely solid, APPROVE it. Only
`blocker` and `major` issues should block the PR; `minor` issues are noted but do
not by themselves require changes. Judge ONLY the change in the diff against the
ticket — do not demand scope beyond the ticket."""

_VERDICT_CONTRACT = """Respond with STRICT JSON ONLY — no prose, no fences:
{
  "verdict": "approve" | "request_changes",
  "summary": "<1-3 sentences: your overall read of the change>",
  "issues": [
    {
      "severity": "blocker" | "major" | "minor",
      "location": "<file path and function/area or line>",
      "problem": "<what is wrong, concretely>",
      "fix": "<the specific change that would resolve it>"
    }
  ]
}
Set verdict to "request_changes" if and only if there is at least one blocker or
major issue; otherwise "approve" (issues may still list minor notes). Use an empty
issues array when the change is clean."""


def _clip_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return (
        diff[:_MAX_DIFF_CHARS]
        + f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} chars; review what is shown ...]"
    )


def build_review_prompt(*, goal: str, kind: str, diff: str) -> str:
    return "\n\n".join(
        [
            _REVIEW_RULES,
            f"TICKET ({kind}):\n{goal}",
            f"DIFF UNDER REVIEW:\n{_clip_diff(diff)}",
            _VERDICT_CONTRACT,
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
