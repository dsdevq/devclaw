"""Eval failure-analysis judge — turn "what went wrong?" into a tagged verdict.

After a build run, this reads the spec, the task DAG, a digest of the OpenHands
events, and the acceptance result, then asks ``claude`` to diagnose what happened
and bucket it into a controlled vocabulary of failure modes. Categorizing (not
just free-text) is what lets you aggregate across N runs — "5/10 failed on
`acceptance_gap`, 2 on `engine_failure`" tells you where to spend effort.

Cognition is `claude` (same as the planner / scope grill); the prompt-building,
response validation, and aggregation are pure, so this is unit-testable with a stub.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Awaitable, Callable, Optional

from ..planner import PlannerError, claude_with_model, extract_json

#: the judge buckets a run into a fixed vocabulary + a suggestion — bounded
#: classification, so Haiku is the right tier. Empty → account default.
JUDGE_MODEL = os.environ.get("DEVCLAW_JUDGE_MODEL", "haiku") or None
#: default cognition caller for the judge, bound to the judge tier
judge_caller = claude_with_model(JUDGE_MODEL, role="judge")

#: controlled failure-mode vocabulary — keep stable so verdicts aggregate.
CATEGORIES = (
    "success",  # clean pass
    "planning_error",  # the DAG was wrong: missing / extra / misordered tasks
    "incomplete_build",  # tasks didn't finish (not an engine crash)
    "constraint_violation",  # ignored a spec constraint / built the wrong interface
    "acceptance_gap",  # built something, but it doesn't satisfy the acceptance contract
    "engine_failure",  # sandbox / docker / runner crash — not the agent's fault
    "stuck",  # no progress / loop / timed out
    "other",
)

def events_digest(events: list[dict], limit: int = 15) -> str:
    """Bound the event stream for the prompt: counts by type, plus the tail and
    any error-ish events verbatim (trimmed)."""
    if not events:
        return "(no events)"
    by_type = Counter(e.get("type", "?") for e in events)
    head = "event counts: " + ", ".join(f"{t}×{n}" for t, n in by_type.most_common())
    tail = events[-limit:]

    def _line(e: dict) -> str:
        payload = e.get("payloadJson") or e.get("payload") or ""
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        return f"- [{e.get('type', '?')}/{e.get('source', '?')}] {payload[:240]}"

    return head + "\n" + "\n".join(_line(e) for e in tail)


def _tasks_digest(tasks: list[dict]) -> str:
    if not tasks:
        return "(no tasks — planning never produced a DAG)"
    lines = []
    for t in tasks:
        ms = f" [{t['milestone']}]" if t.get("milestone") else ""
        err = f" ERROR: {t['error']}" if t.get("error") else ""
        lines.append(f"- ({t.get('status', '?')}){ms} {t.get('goal', '')[:160]}{err}")
    return "\n".join(lines)


def build_judge_prompt(
    *,
    spec: Optional[str],
    program: dict,
    tasks: list[dict],
    events: list[dict],
    acceptance: Optional[bool],
    accept_output: str = "",
) -> str:
    from ..prompts import load_prompt

    acc = {True: "PASSED", False: "FAILED", None: "NOT RUN (build did not complete)"}[acceptance]
    return "\n\n".join(
        [
            load_prompt("eval-judge"),
            f"SPEC:\n{spec or '(no spec provided)'}",
            f"PROGRAM STATUS: {program.get('status', 'unknown')}",
            f"TASK DAG:\n{_tasks_digest(tasks)}",
            f"EVENT DIGEST:\n{events_digest(events)}",
            f"ACCEPTANCE: {acc}\n{accept_output[-1000:] if accept_output else ''}".strip(),
        ]
    )


def validate_verdict(parsed: object) -> dict:
    if not isinstance(parsed, dict):
        raise PlannerError("Judge response must be a JSON object")
    category = parsed.get("category")
    if category not in CATEGORIES:
        raise PlannerError(f"Judge category must be one of {CATEGORIES}, got {category!r}")
    verdict = parsed.get("verdict")
    if verdict not in ("pass", "fail"):
        raise PlannerError(f"Judge verdict must be 'pass' or 'fail', got {verdict!r}")
    diagnosis = parsed.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise PlannerError("Judge missing a diagnosis")
    conf = parsed.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.0
    suggestion = parsed.get("suggestion")
    return {
        "category": category,
        "verdict": verdict,
        "diagnosis": diagnosis.strip(),
        "suggestion": suggestion.strip() if isinstance(suggestion, str) else "",
        "confidence": conf,
    }


async def judge_run(
    *,
    spec: Optional[str],
    program: dict,
    tasks: list[dict],
    events: list[dict],
    acceptance: Optional[bool],
    accept_output: str = "",
    claude_caller: Callable[[str], Awaitable[str]] = judge_caller,
) -> dict:
    """Diagnose one run into a validated verdict. ``claude_caller`` is injected so
    tests can stub the subprocess."""
    prompt = build_judge_prompt(
        spec=spec, program=program, tasks=tasks, events=events,
        acceptance=acceptance, accept_output=accept_output,
    )
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Judge JSON parse failed: {err}", raw) from err
    return validate_verdict(parsed)


def summarize_verdicts(verdicts: list[dict]) -> dict:
    """Aggregate verdicts into a failure-mode breakdown across runs — where the
    effort should go."""
    counts = Counter(v.get("category", "other") for v in verdicts)
    return {
        "runs_judged": len(verdicts),
        "by_category": dict(counts.most_common()),
        "top_failure_mode": next(
            (cat for cat, _ in counts.most_common() if cat != "success"), None
        ),
    }
