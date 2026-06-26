"""Direction evaluator — the JSON contract + verdict-mapping safety nets."""

from __future__ import annotations

import json

import pytest

from devclaw.goal.evaluator import GoalEvalError, build_prompt, evaluate, extract_json, validate
from devclaw.goal.models import Goal, GoalStatus


def test_extract_and_bad_verdict():
    assert '"verdict"' in extract_json('{"verdict":"achieved"}')
    with pytest.raises(GoalEvalError):
        validate({"verdict": "vibes"})
    with pytest.raises(GoalEvalError):
        validate("not a dict")


def test_each_valid_verdict():
    for v in ("on_track", "achieved", "stalled"):
        assert validate({"verdict": v, "rationale": "x"}).verdict == v


def test_off_track_requires_corrections_else_softened():
    # off_track WITH corrections stays off_track
    r = validate({"verdict": "off_track", "rationale": "drifting", "corrections": ["redo X"]})
    assert r.verdict == "off_track"
    assert r.corrections == ["redo X"]
    # off_track with NO corrections is softened to on_track (not actionable)
    r2 = validate({"verdict": "off_track", "rationale": "meh", "corrections": []})
    assert r2.verdict == "on_track"


def test_needs_human_backfills_question_from_rationale():
    r = validate({"verdict": "needs_human", "rationale": "which cloud provider?"})
    assert r.verdict == "needs_human"
    assert r.question == "which cloud provider?"


def _goal():
    return Goal(
        id="g", objective="ship a health endpoint", cadence="1d", engine="devclaw",
        workspace_dir="/ws", done_when="/health returns 200 and is tested",
        backlog=["add /health"],
    )


def test_done_gate_prompt_includes_review():
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report="the repo has /health and a passing test", at_done_gate=True,
    )
    assert "DONE-GATE" in prompt
    assert "read-only review" in prompt.lower()
    assert "the repo has /health" in prompt


@pytest.mark.asyncio
async def test_evaluate_roundtrip_with_injected_caller():
    calls = {"n": 0}

    async def caller(prompt: str) -> str:
        calls["n"] += 1
        return json.dumps({"verdict": "achieved", "rationale": "done_when met: /health tested"})

    ev = await evaluate(_goal(), GoalStatus(), "log", "deliveries", claude_caller=caller)
    assert ev.verdict == "achieved"
    assert calls["n"] == 1


def test_done_gate_prompt_includes_spec_when_present():
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report="repo has /health", at_done_gate=True,
        spec="MUST expose /health AND /ready; auth required.",
    )
    assert "Agreed spec" in prompt and "/ready" in prompt


# ---- per-clause evidence contract (the 2026-06-25 trash-PR safety net) ----


def test_goal_evaluator_prompt_carries_clause_decomposition_directive():
    """The prompt MUST tell the model to decompose done_when into atomic
    clauses and demand specific repo evidence per clause — this is the
    behaviour that prevents the 'all stubs counted as done' failure mode."""
    prompt = build_prompt(_goal(), GoalStatus(), "log", "deliveries", at_done_gate=True)
    assert "DECOMPOSE" in prompt or "decompose" in prompt.lower()
    assert "atomic clauses" in prompt.lower() or "atomic clause" in prompt.lower()
    assert "evidence" in prompt.lower()
    # the strict rule that prevents the trash-PR class
    assert "achieved" in prompt.lower()


def test_done_gate_achieved_without_clauses_is_downgraded():
    # Belt-and-suspenders: even with a strict prompt the model can claim
    # 'achieved' without producing per-clause evidence. The validator
    # downgrades to off_track with a forcing correction.
    r = validate(
        {"verdict": "achieved", "rationale": "looks good"},
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert r.corrections, "expected a forcing correction asking for clauses"
    assert "clause" in r.corrections[0].lower()


def test_done_gate_achieved_with_unsatisfied_clause_is_downgraded():
    # 'achieved' with at least one unsatisfied clause must downgrade and
    # surface a per-clause correction.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "shipped",
            "clauses": [
                {
                    "clause": "/health returns 200",
                    "satisfied": True,
                    "evidence": "src/Health.cs:12 returns OK; HealthTests.cs:8 asserts 200",
                },
                {
                    "clause": "/health is tested",
                    "satisfied": False,
                    "evidence": "missing — should live in tests/HealthTests.cs",
                },
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    # the unsatisfied clause must surface as a correction
    assert any("/health is tested" in c for c in r.corrections)
    # clauses are preserved on the result for downstream visibility
    assert len(r.clauses) == 2
    assert r.clauses[1].satisfied is False


def test_done_gate_achieved_with_partial_evidence_is_downgraded():
    # 'partial' (string) coerces to satisfied=False — partial doesn't count.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "mostly",
            "clauses": [
                {"clause": "feature A", "satisfied": "yes", "evidence": "src/A.cs"},
                {"clause": "feature B", "satisfied": "partial", "evidence": "src/B.cs (incomplete)"},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert any("feature B" in c for c in r.corrections)


def test_done_gate_achieved_clause_with_no_evidence_is_downgraded():
    # satisfied=True but empty evidence → still downgraded (evidence contract).
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "shipped",
            "clauses": [
                {"clause": "feature A", "satisfied": True, "evidence": ""},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert any("feature A" in c for c in r.corrections)


def test_done_gate_achieved_with_all_clauses_satisfied_stays_achieved():
    # The HAPPY path: every clause satisfied with real evidence → achieved.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "all clauses met",
            "clauses": [
                {
                    "clause": "/health returns 200",
                    "satisfied": True,
                    "evidence": "src/Health.cs:12; HealthTests.cs:8",
                },
                {
                    "clause": "/health is tested",
                    "satisfied": True,
                    "evidence": "HealthTests.cs:8 Health_Returns200",
                },
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "achieved"
    assert len(r.clauses) == 2
    assert all(c.satisfied for c in r.clauses)


def test_pre_done_gate_achieved_is_not_strict():
    # Outside the done-gate, achieved doesn't require clauses (mid-goal
    # evaluator never returns achieved in practice, but the validator must
    # not reject it). Behaviour stays as the existing soft contract.
    r = validate({"verdict": "achieved", "rationale": "wip"})
    assert r.verdict == "achieved"


def test_off_track_at_done_gate_preserves_clauses():
    # When the model itself returns off_track (with corrections), the clauses
    # it produced are still preserved for visibility.
    r = validate(
        {
            "verdict": "off_track",
            "rationale": "one clause missing",
            "corrections": ["[clause 2] add the missing endpoint"],
            "clauses": [
                {"clause": "feature A", "satisfied": True, "evidence": "src/A.cs"},
                {"clause": "feature B", "satisfied": False, "evidence": "missing"},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert len(r.clauses) == 2
    assert r.corrections == ["[clause 2] add the missing endpoint"]


def test_eval_prompt_omits_spec_section_when_absent():
    prompt = build_prompt(_goal(), GoalStatus(), "log", "deliveries")
    assert "Agreed spec" not in prompt
