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


#: Headroom for the review report inside the evaluator prompt. The worker's
#: ``agent_output`` is the SDK's full captured-stdout transcript — banners,
#: prompt echo, per-tool-call panels (each shown twice: status=pending then
#: status=completed) — and is regularly 60–160 KB. The actual per-clause
#: report the brief asks for lives at the END. Truncating from the head
#: literally kept the EMPTY template lines from the brief plus the first few
#: `status=pending` tool-call panels — and the evaluator concluded "review
#: was cut off mid-exploration." Keep enough tail to fit the report comfortably.
_REVIEW_REPORT_KEEP = 20000


def _extract_review_report(raw: str) -> str:
    """Pull the agent's actual per-clause report out of the worker's captured
    stdout. The brief mandates a ``## Per-clause evidence`` section followed by
    ``## Summary`` and ``## Risks not in done_when`` — the LAST occurrence of
    that header is the filled-in report (an earlier occurrence, if present, is
    the prompt's own format template echoed back in the SDK's user-message
    panel). When the header isn't present (truly cut-off run), fall back to the
    tail — the tail still preserves any partial work, while the head was
    always just banner + tool-call decoration.

    Centralized here, not in the runner, because (a) all historical task rows
    on disk hold the un-cleaned ``agent_output`` and the done-gate must still
    read them correctly, and (b) the parsing is purely defensive — even a
    future cleaner runner can only emit a best-effort extraction; the
    evaluator should still cope with both shapes."""
    if not raw:
        return ""
    header = "## Per-clause evidence"
    idx = raw.rfind(header)
    if idx == -1:
        return raw[-_REVIEW_REPORT_KEEP:]
    section = raw[idx:]
    return section[:_REVIEW_REPORT_KEEP]


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
    if goal.stub_acceptable:
        parts += [
            "\nstub_acceptable (tools/capabilities the OWNER explicitly authorized as `not_yet_available` stubs — a stub-shaped clause is ONLY satisfiable when the clause names one of these):",
            "\n".join(f"  - {t}" for t in goal.stub_acceptable),
        ]
    else:
        parts.append(
            "\nstub_acceptable: (empty — the owner has NOT authorized any stubs. "
            "If a clause's only evidence is a `not_yet_available` stub, that clause "
            "is UNSATISFIED regardless of how the tool is shaped.)"
        )
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
            _extract_review_report(review_report),
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


#: case-insensitive substrings that mean "this clause is being satisfied by a
#: stub, not by real work." The mechanical check below flips a satisfied clause
#: to unsatisfied when one of these is present in clause+evidence AND the
#: owner did not authorize a stub for the named tool.
_STUB_MARKERS = ("not_yet_available", "notyetavailable", "legit_stub")


def _looks_like_stub(text: str) -> bool:
    s = text.lower()
    return any(m in s for m in _STUB_MARKERS)


_VERB_PREFIXES = ("get", "list", "fetch", "read", "describe", "show")


def _norm(s: str) -> str:
    """Aggressive identifier normalization for cross-naming-convention match:
    lowercase, strip ``_ - `` and whitespace. Lets ``get_cashflow_report`` find
    its evidence in ``CashflowReportStub.cs`` (different naming convention,
    same underlying capability)."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _slug_variants(name: str) -> list[str]:
    """For a stub_acceptable entry, return the normalized forms we'll search
    for in the clause/evidence. Includes the verb-stripped variant so an MCP
    tool slug like ``get_cashflow_report`` matches a C# evidence string like
    ``CashflowReportStub.cs`` (which has no ``get`` prefix)."""
    n = _norm(name)
    if not n:
        return []
    variants = [n]
    for prefix in _VERB_PREFIXES:
        if n.startswith(prefix) and len(n) > len(prefix) + 2:
            variants.append(n[len(prefix):])
            break
    return variants


def _stub_is_authorized(clause: ClauseVerdict, stub_acceptable: list[str]) -> bool:
    """A stub-shaped clause is authorized when the owner's ``stub_acceptable``
    list names a tool/capability that appears in the clause text or its
    evidence. Match is case-insensitive AND naming-convention-insensitive:
    tool slug ``get_cashflow_report`` authorizes evidence mentioning
    ``CashflowReportStub.cs`` or ``cashflow report`` or any other casing the
    repo/model uses."""
    if not stub_acceptable:
        return False
    haystack = _norm(f"{clause.clause}\n{clause.evidence}")
    for name in stub_acceptable:
        for variant in _slug_variants(name):
            if variant in haystack:
                return True
    return False


def validate(parsed: object, *, at_done_gate: bool = False, stub_acceptable: list[str] | None = None) -> EvalResult:
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
        # Mechanical stub-policy enforcement: a satisfied clause whose evidence
        # is structurally a stub (not_yet_available payload, *Stub class, etc.)
        # is only allowed when the owner's stub_acceptable lists the tool the
        # clause refers to. Otherwise we flip it to unsatisfied — the safety
        # net for the 2026-06-26 v5 failure mode where the agent shipped four
        # stubs as "done" and the gate stamped them green.
        allowed_stubs = list(stub_acceptable or [])
        normalized: list[ClauseVerdict] = []
        for c in clauses:
            if c.satisfied and c.evidence and _looks_like_stub(f"{c.clause}\n{c.evidence}"):
                if not _stub_is_authorized(c, allowed_stubs):
                    normalized.append(ClauseVerdict(
                        clause=c.clause, satisfied=False,
                        evidence=(
                            f"unauthorized stub — evidence ({c.evidence!s}) is a "
                            f"not_yet_available stub but the goal's stub_acceptable "
                            f"does not list this tool. Either implement the real "
                            f"capability or add the tool name to stub_acceptable in "
                            f"goal.yaml to explicitly accept the stub."
                        ),
                    ))
                    continue
            normalized.append(c)
        clauses = normalized
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
    return validate(parsed, at_done_gate=at_done_gate, stub_acceptable=goal.stub_acceptable)


def default_caller() -> ClaudeCaller:
    """Production cognition caller bound to the evaluator tier (lazy import)."""
    from ..planner import claude_with_model

    return claude_with_model(GOAL_EVAL_MODEL, role="evaluator")
