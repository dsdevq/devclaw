"""Direction evaluator — the JSON contract + verdict-mapping safety nets."""

from __future__ import annotations

import json

import pytest

from devclaw.goal_evaluator import GoalEvalError, build_prompt, evaluate, extract_json, validate
from devclaw.goal_models import Goal, GoalStatus


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
