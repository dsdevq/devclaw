"""The direction evaluator — "is this goal going the right way?".

This is the layer the old shipped-PRs-vs-backlog "done" check could not be. That
check was shallow: a PR can be gate-green but wrong; the backlog can drift from
the real intent; *done* is not the same as *good*. Now that the evaluator (devclaw)
sits right next to the repo and the execution context, it judges direction from
GROUNDED ARTIFACTS — the agent's own output, the verify-gate verdicts, the PRs,
and (at the done-gate) a read-only review of the actual repo against done_when —
not from counting backlog items.

It runs as a SEPARATE, less-frequent cognition step from the next-action planner
(the mechanism/cognition split applied to evaluation itself): the cheap per-tick
progress check and per-delivery evidence capture cost ~0 tokens and gate when the
evaluator runs, so direction is judged periodically and at the moment of closing,
never on every tick.

The verdict drives the loop, it doesn't just report:
- ``achieved``    → the goal may close ``done`` (only path to done — the planner's
                    "done" is merely a proposal).
- ``off_track``   → ``corrections`` are written to inbox.md as steering; the
                    next-action planner picks them up and the goal keeps going.
- ``stalled``     → block + notify (thrash / repeated failure that won't self-fix).
- ``needs_human`` → block + notify with a specific question.
- ``on_track``    → record and continue.
"""

from __future__ import annotations

import json
import os
import re
from typing import Awaitable, Callable, Optional

from .models import ClauseVerdict, EvalResult, Goal, GoalStatus

ClaudeCaller = Callable[[str], Awaitable[str]]

_VALID_VERDICTS = {"on_track", "off_track", "achieved", "stalled", "needs_human"}

#: the evaluator's model tier. Judging delivered work against intent is more
#: load-bearing than picking the next step → defaults a notch up is reasonable,
#: but sonnet is the cost-conscious default; bump to opus per goal via env.
GOAL_EVAL_MODEL = os.environ.get("DEVCLAW_GOAL_EVAL_MODEL", "sonnet") or None


class GoalEvalError(Exception):
    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def build_prompt(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    deliveries: str,
    *,
    review_report: Optional[str] = None,
    at_done_gate: bool = False,
    spec: str = "",
) -> str:
    from ..prompts import load_prompt

    backlog = "\n".join(f"  - {b}" for b in goal.backlog) or "  (none listed)"
    parts = [
        load_prompt("goal-evaluator"),
        "\n## Goal",
        f"objective: {goal.objective}",
        f"done_when: {goal.done_when or '(not specified)'}",
        "backlog (the starting work-list — NOT the definition of done):",
        backlog,
    ]
    if spec:
        parts += [
            "\n## Agreed spec (the contract aligned with the owner — judge done against THIS)",
            spec[:4000],
        ]
    if at_done_gate:
        parts.append(
            "\n## CONTEXT: this is the DONE-GATE.\n"
            "The next-action planner believes the goal is complete. Decide whether "
            "done_when is TRULY satisfied. Return 'achieved' only if the evidence "
            "and the repo review below actually show the objective met; otherwise "
            "'off_track' with the corrections still needed."
        )
    parts += [
        "\n## What has actually shipped (grounded deliveries)",
        deliveries or "(nothing delivered yet)",
        "\n## Recent event log",
        recent_log or "(no events yet)",
    ]
    if review_report:
        parts += [
            "\n## Fresh read-only review of the current repo vs done_when",
            review_report[:6000],
        ]
    parts.append("\nReturn the JSON now.")
    return "\n".join(parts)


def extract_json(text: str) -> str:
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return trimmed
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", trimmed)
    if fence and fence.group(1):
        return fence.group(1)
    first, last = trimmed.find("{"), trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    raise GoalEvalError("No JSON object found in evaluator response", text)


def _parse_clauses(raw: object) -> list[ClauseVerdict]:
    """Parse the model's ``clauses`` array. Tolerant of shape drift: drops
    entries that aren't dicts, coerces bool-ish ``satisfied`` values
    (true/false, "yes"/"no", "partial" → False)."""
    if not isinstance(raw, list):
        return []
    out: list[ClauseVerdict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        clause = str(entry.get("clause", "")).strip()
        if not clause:
            continue
        sat_raw = entry.get("satisfied")
        if isinstance(sat_raw, bool):
            satisfied = sat_raw
        elif isinstance(sat_raw, str):
            # "yes" → True; "partial" / "no" / anything else → False (the strict
            # contract: partial doesn't satisfy a clause at the done-gate)
            satisfied = sat_raw.strip().lower() == "yes"
        else:
            satisfied = False
        evidence = str(entry.get("evidence", "")).strip()
        out.append(ClauseVerdict(clause=clause, satisfied=satisfied, evidence=evidence))
    return out


def validate(parsed: object, *, at_done_gate: bool = False) -> EvalResult:
    """Validate + normalize the model's evaluation. When ``at_done_gate=True``,
    ``achieved`` requires every clause in ``clauses`` to be ``satisfied=True``
    with non-empty ``evidence`` — otherwise the verdict is downgraded to
    ``off_track`` with one correction per unsatisfied clause (the safety net
    that closes the 2026-06-25 "stub everything" failure mode, where the model
    can still ignore the strict prompt and stamp ``achieved`` on vibes)."""
    if not isinstance(parsed, dict):
        raise GoalEvalError("Eval must be a JSON object")
    verdict = parsed.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise GoalEvalError(f"verdict must be one of {_VALID_VERDICTS}, got {verdict!r}")
    rationale = str(parsed.get("rationale", "")).strip()
    raw_corr = parsed.get("corrections") or []
    corrections = [str(c).strip() for c in raw_corr if str(c).strip()] if isinstance(raw_corr, list) else []
    question = str(parsed.get("question", "")).strip()
    clauses = _parse_clauses(parsed.get("clauses"))
    if verdict == "needs_human" and not question:
        # tolerate a model that put the ask in rationale rather than question
        question = rationale or "the evaluator needs a human decision (no question given)"
    if verdict == "off_track" and not corrections:
        # off_track is only actionable with corrections; treat a bare off_track as
        # a soft on_track so we don't silently stall without steering.
        return EvalResult(verdict="on_track", rationale=rationale or "no corrections given", clauses=clauses)
    # Done-gate strictness: achieved MUST be backed by per-clause evidence; the
    # model can technically still claim achieved with no clauses (or with some
    # unsatisfied), so we re-check here and downgrade with derived corrections.
    if at_done_gate and verdict == "achieved":
        if not clauses:
            return EvalResult(
                verdict="off_track",
                rationale=(
                    rationale or "evaluator returned 'achieved' but provided no per-clause "
                    "evidence — the done-gate requires explicit clause-by-clause grading."
                ),
                corrections=[
                    "Return a per-clause `clauses` array with satisfied + evidence for "
                    "every atomic done_when requirement; do not claim 'achieved' without "
                    "it."
                ],
                clauses=clauses,
            )
        unsatisfied = [c for c in clauses if not c.satisfied or not c.evidence]
        if unsatisfied:
            derived = [
                f"[clause: {c.clause}] {c.evidence or 'no evidence provided'} — "
                f"address this before declaring done."
                for c in unsatisfied
            ]
            return EvalResult(
                verdict="off_track",
                rationale=(
                    rationale or f"{len(unsatisfied)} of {len(clauses)} done_when "
                    "clause(s) lack confirmed evidence."
                ),
                corrections=derived,
                clauses=clauses,
            )
    return EvalResult(
        verdict=verdict, rationale=rationale, corrections=corrections,
        question=question, clauses=clauses,
    )


async def evaluate(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    deliveries: str,
    *,
    claude_caller: ClaudeCaller,
    review_report: Optional[str] = None,
    at_done_gate: bool = False,
    spec: str = "",
) -> EvalResult:
    """Run the direction evaluation. ``claude_caller`` is injected so tests stub
    the LLM. Pass ``review_report`` + ``at_done_gate`` when judging a done proposal;
    ``spec`` (the waiter-provided scope contract) when one exists, so done is
    judged against it."""
    prompt = build_prompt(
        goal, status, recent_log, deliveries,
        review_report=review_report, at_done_gate=at_done_gate, spec=spec,
    )
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as exc:
        raise GoalEvalError(f"evaluator emitted invalid JSON: {exc}", raw) from exc
    return validate(parsed, at_done_gate=at_done_gate)


def default_caller() -> ClaudeCaller:
    """Production cognition caller bound to the evaluator tier (lazy import)."""
    from ..planner import claude_with_model

    return claude_with_model(GOAL_EVAL_MODEL, role="evaluator")
