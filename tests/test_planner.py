"""Planner unit tests — extract_json, validate_plan (topo/cycles/refs), plan_goal."""

import json

import pytest

from devclaw.planner import (
    PlannerError,
    extract_json,
    plan_goal,
    plan_spec,
    validate_plan,
)


# ---- extract_json ----


def test_extract_json_leading_whitespace():
    assert extract_json('   {"a":1}  ') == '{"a":1}'


def test_extract_json_fenced():
    assert json.loads(extract_json('```json\n{"tasks":[]}\n```')) == {"tasks": []}


def test_extract_json_prose_preface_and_suffix():
    out = extract_json('Sure! Here:\n{"tasks":[{"key":"t1"}]}\nDone.')
    assert json.loads(out) == {"tasks": [{"key": "t1"}]}


def test_extract_json_no_json_raises():
    with pytest.raises(PlannerError):
        extract_json("no json here")


# ---- validate_plan ----


def _plan(*tasks):
    return {"tasks": list(tasks)}


def test_single_task_no_deps():
    out = validate_plan(_plan({"key": "t1", "goal": "do", "kind": "implement_feature"}))
    assert len(out) == 1 and out[0].key == "t1"


def test_default_kind_is_implement_feature():
    out = validate_plan(_plan({"key": "t1", "goal": "do"}))
    assert out[0].kind == "implement_feature"


def test_linear_chain_orders_topologically():
    out = validate_plan(
        _plan(
            {"key": "c", "goal": "g", "depends_on": ["b"]},
            {"key": "b", "goal": "g", "depends_on": ["a"]},
            {"key": "a", "goal": "g"},
        )
    )
    assert [t.key for t in out] == ["a", "b", "c"]


def test_diamond_dag_ordered():
    out = validate_plan(
        _plan(
            {"key": "d", "goal": "g", "depends_on": ["b", "c"]},
            {"key": "b", "goal": "g", "depends_on": ["a"]},
            {"key": "c", "goal": "g", "depends_on": ["a"]},
            {"key": "a", "goal": "g"},
        )
    )
    order = [t.key for t in out]
    assert order[0] == "a" and order[-1] == "d"
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_cycle_rejected():
    with pytest.raises(PlannerError, match="cycle"):
        validate_plan(
            _plan(
                {"key": "a", "goal": "g", "depends_on": ["b"]},
                {"key": "b", "goal": "g", "depends_on": ["a"]},
            )
        )


def test_self_dep_rejected():
    with pytest.raises(PlannerError, match="depends on itself"):
        validate_plan(_plan({"key": "a", "goal": "g", "depends_on": ["a"]}))


def test_dangling_ref_rejected():
    with pytest.raises(PlannerError, match="unknown key"):
        validate_plan(_plan({"key": "a", "goal": "g", "depends_on": ["ghost"]}))


def test_duplicate_key_rejected():
    with pytest.raises(PlannerError, match="Duplicate"):
        validate_plan(_plan({"key": "a", "goal": "g"}, {"key": "a", "goal": "g2"}))


def test_invalid_kind_rejected():
    with pytest.raises(PlannerError, match="invalid kind"):
        validate_plan(_plan({"key": "a", "goal": "g", "kind": "delete_everything"}))


def test_empty_list_rejected():
    with pytest.raises(PlannerError):
        validate_plan(_plan())


def test_non_array_rejected():
    with pytest.raises(PlannerError):
        validate_plan({"tasks": "nope"})


def test_missing_goal_rejected():
    with pytest.raises(PlannerError, match="missing 'goal'"):
        validate_plan(_plan({"key": "a"}))


def test_milestone_optional_defaults_none():
    out = validate_plan(_plan({"key": "a", "goal": "g"}))
    assert out[0].milestone is None


def test_milestone_parsed_when_present():
    out = validate_plan(_plan({"key": "a", "goal": "g", "milestone": "M1 scaffold"}))
    assert out[0].milestone == "M1 scaffold"


# ---- plan_spec (with stubbed claude) ----


async def test_plan_spec_decomposes_into_milestoned_dag():
    async def stub(prompt):
        assert "APPROVED SPEC" in prompt  # the spec is embedded in the prompt
        return json.dumps(
            {
                "tasks": [
                    {"key": "m1", "goal": "scaffold", "milestone": "M1", "kind": "implement_feature"},
                    {"key": "m2", "goal": "auth", "milestone": "M2", "depends_on": ["m1"]},
                ]
            }
        )

    out = await plan_spec("# spec\n## Milestones\n- M1\n- M2", "/ws", stub)
    assert [t.key for t in out] == ["m1", "m2"]  # topo order
    assert out[0].milestone == "M1" and out[1].milestone == "M2"


# ---- plan_goal (with stubbed claude) ----


async def test_plan_goal_parses_fenced_json():
    async def stub(_prompt):
        return '```json\n{"tasks":[{"key":"t1","goal":"do it"}]}\n```'

    out = await plan_goal("goal", "/ws", stub)
    assert len(out) == 1 and out[0].goal == "do it"


async def test_plan_goal_bubbles_parse_error():
    async def stub(_prompt):
        return "not json at all"

    with pytest.raises(PlannerError):
        await plan_goal("goal", "/ws", stub)


async def test_plan_goal_full_dag_roundtrip_preserves_order():
    async def stub(_prompt):
        return json.dumps(
            {
                "tasks": [
                    {"key": "two", "goal": "g", "depends_on": ["one"]},
                    {"key": "one", "goal": "g"},
                ]
            }
        )

    out = await plan_goal("goal", "/ws", stub)
    assert [t.key for t in out] == ["one", "two"]
