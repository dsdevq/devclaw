"""Fixture loader + evaluator runner primitives for the cognition eval suite.

Fixtures are JSON files under ``tests/cognition/fixtures/<module>/`` and match the
schema documented in the sibling README.  ``load_evaluator_fixtures`` yields
each one already parsed into the ``Goal`` + ``GoalStatus`` objects the live
evaluator consumes, so tests never touch raw JSON.

The live-run helper (``run_evaluator_live``) is only imported/used by the
opt-in cognition test and only when ``DEVCLAW_RUN_COGNITION_EVALS=1`` — normal
pytest never triggers a real Anthropic call.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from devclaw.goal.evaluator import build_prompt, evaluate, validate
from devclaw.goal.models import EvalResult, Goal, GoalStatus

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class EvaluatorFixture:
    """One evaluator fixture, materialized into ready-to-use objects.

    ``expected_verdict`` and ``expected_hints`` are the human-graded target;
    ``canned_model_output`` is what a well-behaved model *would* have returned,
    used by the mechanism guard to verify parsing/validation round-trips it.
    """

    name: str
    module: str
    source: str
    notes: str
    goal: Goal
    status: GoalStatus
    recent_log: str
    deliveries: str
    review_report: Optional[str]
    at_done_gate: bool
    spec: str
    expected_verdict: str
    expected_rationale_hints: list[str]
    expected_correction_hints: list[str]
    expected_notes: str
    canned_model_output: dict


def _build_goal(raw: dict) -> Goal:
    """Materialize the ``inputs.goal`` block into a real Goal.  Only names
    the fields the evaluator actually reads; everything else is defaulted."""
    return Goal(
        id=str(raw["id"]),
        objective=str(raw["objective"]),
        cadence=str(raw.get("cadence", "2h")),
        engine="devclaw",
        workspace_dir=str(raw.get("workspace_dir", "/repos/example")),
        repo_url=raw.get("repo_url"),
        verify_cmd=raw.get("verify_cmd"),
        open_pr=bool(raw.get("open_pr", True)),
        done_when=str(raw.get("done_when", "")),
        backlog=list(raw.get("backlog") or []),
        stub_acceptable=list(raw.get("stub_acceptable") or []),
        skills_required=list(raw.get("skills_required") or []),
    )


def _build_status(raw: dict) -> GoalStatus:
    """Materialize the ``inputs.status`` block into a GoalStatus.  All fields
    optional — evaluator only reads a handful, but keeping the shape realistic
    means fixtures double as documentation of the state at eval time."""
    return GoalStatus(
        phase=raw.get("phase", "verifying"),
        lifecycle=raw.get("lifecycle"),
        next=str(raw.get("next", "")),
        last_plan_at=raw.get("last_plan_at"),
        last_tick_at=raw.get("last_tick_at"),
        inbox_cursor=int(raw.get("inbox_cursor", 0)),
        actions_dispatched=int(raw.get("actions_dispatched", 0)),
        deliveries_since_eval=int(raw.get("deliveries_since_eval", 0)),
        last_eval_verdict=raw.get("last_eval_verdict"),
        last_eval_at=raw.get("last_eval_at"),
        last_eval_note=str(raw.get("last_eval_note", "")),
        last_progress_at=raw.get("last_progress_at"),
        no_progress_notified=bool(raw.get("no_progress_notified", False)),
    )


def load_evaluator_fixtures() -> list[EvaluatorFixture]:
    """Load every JSON fixture under ``fixtures/evaluator/`` into materialized
    ``EvaluatorFixture`` objects.  Sorted by name for stable test IDs."""
    root = FIXTURES_ROOT / "evaluator"
    if not root.exists():
        return []
    out: list[EvaluatorFixture] = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text())
        inputs = raw.get("inputs") or {}
        expected = raw.get("expected") or {}
        out.append(EvaluatorFixture(
            name=str(raw["name"]),
            module=str(raw.get("module", "evaluator")),
            source=str(raw.get("source", "unspecified")),
            notes=str(raw.get("notes", "")),
            goal=_build_goal(inputs["goal"]),
            status=_build_status(inputs.get("status") or {}),
            recent_log=str(inputs.get("recent_log", "")),
            deliveries=str(inputs.get("deliveries", "")),
            review_report=inputs.get("review_report"),
            at_done_gate=bool(inputs.get("at_done_gate", False)),
            spec=str(inputs.get("spec", "")),
            expected_verdict=str(expected["verdict"]),
            expected_rationale_hints=list(expected.get("must_contain_rationale_hints") or []),
            expected_correction_hints=list(expected.get("must_contain_correction_hints") or []),
            expected_notes=str(expected.get("notes", "")),
            canned_model_output=dict(raw.get("canned_model_output") or {}),
        ))
    return out


def build_prompt_for(fixture: EvaluatorFixture) -> str:
    """Same shape the production evaluator sees — used by the mechanism guard
    to verify a fixture's prompt is well-formed before any live call."""
    return build_prompt(
        goal=fixture.goal,
        status=fixture.status,
        recent_log=fixture.recent_log,
        deliveries=fixture.deliveries,
        review_report=fixture.review_report,
        at_done_gate=fixture.at_done_gate,
        spec=fixture.spec,
    )


def validate_canned(fixture: EvaluatorFixture) -> EvalResult:
    """Feed the fixture's ``canned_model_output`` through the real ``validate``
    layer.  The result must reproduce ``expected_verdict`` — if it doesn't, the
    fixture is incoherent (the canned output the human wrote wouldn't survive
    the parser)."""
    return validate(
        fixture.canned_model_output,
        at_done_gate=fixture.at_done_gate,
        stub_acceptable=fixture.goal.stub_acceptable,
    )


def cognition_evals_enabled() -> bool:
    """True when ``DEVCLAW_RUN_COGNITION_EVALS=1``.  Gates every live call."""
    return os.environ.get("DEVCLAW_RUN_COGNITION_EVALS", "0") not in ("0", "", "false", "False")


async def run_evaluator_live(
    fixture: EvaluatorFixture,
    *,
    claude_caller=None,
) -> EvalResult:
    """Call the real ``evaluate()`` against a fixture.  Only invoked from the
    opt-in test.  ``claude_caller`` may be injected for meta-tests; production
    live runs use ``default_caller()``."""
    if claude_caller is None:
        from devclaw.goal.evaluator import default_caller
        claude_caller = default_caller()
    return await evaluate(
        goal=fixture.goal,
        status=fixture.status,
        recent_log=fixture.recent_log,
        deliveries=fixture.deliveries,
        claude_caller=claude_caller,
        review_report=fixture.review_report,
        at_done_gate=fixture.at_done_gate,
        spec=fixture.spec,
    )


def format_verdict(result: EvalResult) -> str:
    """Pretty-print an ``EvalResult`` for human eyeball comparison against a
    fixture's expected verdict.  Used by the live-run test's stdout."""
    lines = [
        f"verdict:    {result.verdict}",
        f"rationale:  {result.rationale}",
    ]
    if result.corrections:
        lines.append("corrections:")
        for c in result.corrections:
            lines.append(f"  - {c}")
    if result.clauses:
        lines.append("clauses:")
        for c in result.clauses:
            mark = "✓" if c.satisfied else "✗"
            lines.append(f"  {mark} {c.clause}")
            if c.evidence:
                lines.append(f"      evidence: {c.evidence}")
    if result.question:
        lines.append(f"question:   {result.question}")
    return "\n".join(lines)


def all_fixture_names() -> Iterable[str]:
    """Just the names, for pytest parametrization IDs."""
    return [f.name for f in load_evaluator_fixtures()]
