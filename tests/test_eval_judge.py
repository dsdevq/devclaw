"""Failure-analysis judge tests — the deterministic core (cognition stubbed)."""

import json

import pytest

from devclaw.quality.eval_judge import (
    CATEGORIES,
    events_digest,
    judge_run,
    summarize_verdicts,
    validate_verdict,
)
from devclaw.planner import PlannerError


# ---- validate_verdict ----


def test_validate_good_verdict():
    v = validate_verdict(
        {"category": "acceptance_gap", "verdict": "fail", "diagnosis": "wrong CLI name", "suggestion": "pin the entrypoint", "confidence": 0.8}
    )
    assert v["category"] == "acceptance_gap" and v["verdict"] == "fail" and v["confidence"] == 0.8


def test_validate_rejects_unknown_category():
    with pytest.raises(PlannerError):
        validate_verdict({"category": "made_up", "verdict": "fail", "diagnosis": "x"})


def test_validate_rejects_bad_verdict():
    with pytest.raises(PlannerError):
        validate_verdict({"category": "success", "verdict": "maybe", "diagnosis": "x"})


def test_validate_clamps_and_defaults_confidence():
    v = validate_verdict({"category": "stuck", "verdict": "fail", "diagnosis": "looped", "confidence": "not a number"})
    assert v["confidence"] == 0.0
    v2 = validate_verdict({"category": "stuck", "verdict": "fail", "diagnosis": "looped", "confidence": 5})
    assert v2["confidence"] == 1.0


def test_validate_requires_diagnosis():
    with pytest.raises(PlannerError):
        validate_verdict({"category": "success", "verdict": "pass", "diagnosis": "  "})


# ---- judge_run (stubbed claude) ----


async def test_judge_run_parses_fenced_verdict():
    async def stub(prompt):
        assert "eval judge" in prompt and "ACCEPTANCE" in prompt  # the judge prompt
        return '```json\n{"category":"engine_failure","verdict":"fail","diagnosis":"docker socket down","suggestion":"check docker","confidence":0.9}\n```'

    v = await judge_run(
        spec="# spec", program={"status": "failed"}, tasks=[{"status": "failed", "goal": "x", "error": "spawn docker"}],
        events=[], acceptance=None, accept_output="", claude_caller=stub,
    )
    assert v["category"] == "engine_failure" and v["verdict"] == "fail"


async def test_judge_run_bubbles_bad_json():
    async def stub(_p):
        return "not json"

    with pytest.raises(PlannerError):
        await judge_run(spec=None, program={}, tasks=[], events=[], acceptance=None, claude_caller=stub)


# ---- summarize + digest ----


def test_summarize_counts_and_top_failure_mode():
    verdicts = [
        {"category": "success"},
        {"category": "acceptance_gap"},
        {"category": "acceptance_gap"},
        {"category": "engine_failure"},
    ]
    s = summarize_verdicts(verdicts)
    assert s["runs_judged"] == 4
    assert s["by_category"]["acceptance_gap"] == 2
    assert s["top_failure_mode"] == "acceptance_gap"  # most common non-success


def test_summarize_all_success_has_no_failure_mode():
    s = summarize_verdicts([{"category": "success"}, {"category": "success"}])
    assert s["top_failure_mode"] is None


def test_events_digest_bounds_and_counts():
    events = [{"type": "ActionEvent", "source": "agent", "payloadJson": "{}"} for _ in range(50)]
    events.append({"type": "ErrorEvent", "source": "env", "payloadJson": '{"msg":"boom"}'})
    d = events_digest(events, limit=5)
    assert "ActionEvent×50" in d
    assert "ErrorEvent" in d  # the tail includes the last events
    assert d.count("\n- ") <= 5  # tail is bounded


def test_categories_stable_vocab():
    assert "success" in CATEGORIES and "engine_failure" in CATEGORIES
