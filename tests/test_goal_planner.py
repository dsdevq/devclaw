"""Goal next-action planner — the JSON contract (folded from goalclaw)."""

from __future__ import annotations

import pytest

from devclaw.goal_planner import GoalPlannerError, extract_json, validate


def test_extract_json_plain():
    assert extract_json('{"decision":"sleep"}') == '{"decision":"sleep"}'


def test_extract_json_fenced():
    raw = "here you go:\n```json\n{\"decision\": \"sleep\"}\n```\n"
    assert '"decision"' in extract_json(raw)


def test_extract_json_none_raises():
    with pytest.raises(GoalPlannerError):
        extract_json("no json here")


def test_validate_act_one_action():
    res = validate(
        {
            "decision": "act",
            "note": "ship health endpoint",
            "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}],
        }
    )
    assert res.decision == "act"
    assert len(res.actions) == 1
    assert res.actions[0].tool == "implement_feature"
    assert res.actions[0].goal == "add /health"
    assert res.actions[0].open_pr is True


def test_validate_act_rejects_multiple_actions():
    with pytest.raises(GoalPlannerError):
        validate(
            {
                "decision": "act",
                "actions": [
                    {"tool": "implement_feature", "goal": "a"},
                    {"tool": "fix_bug", "goal": "b"},
                ],
            }
        )


def test_validate_act_rejects_bad_tool():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "act", "actions": [{"tool": "rm_rf", "goal": "x"}]})


def test_validate_act_rejects_empty_goal():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "act", "actions": [{"tool": "fix_bug", "goal": "  "}]})


def test_validate_blocked_requires_question():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "blocked"})
    res = validate({"decision": "blocked", "question": "which auth provider?"})
    assert res.decision == "blocked"
    assert res.question == "which auth provider?"


def test_validate_sleep_and_done():
    assert validate({"decision": "sleep", "note": "nothing to do"}).decision == "sleep"
    assert validate({"decision": "done", "note": "all merged"}).decision == "done"


def test_validate_bad_decision():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "explode"})
