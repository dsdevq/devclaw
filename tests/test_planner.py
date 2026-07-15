"""Planner unit tests — extract_json, validate_plan (topo/cycles/refs), plan_goal."""

import json
import subprocess

import pytest

from devclaw import planner
from devclaw.planner import (
    PlannerError,
    build_planner_prompt,
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


# ---- plan_goal grounding (triage F6 — the planner sibling of #227) ----

_ONE_TASK = '{"tasks":[{"key":"t1","goal":"do it"}]}'


def _capture_prompt(seen: dict):
    """A stub caller that records the wire prompt and returns a valid plan."""

    async def stub(prompt):
        seen["prompt"] = prompt
        return _ONE_TASK

    return stub


async def test_plan_prompt_includes_repo_context_from_actual_workspace(tmp_path):
    """The wire prompt carries a REPOSITORY CONTEXT snapshot collected from the
    task's ACTUAL workspace — remote, key-file probes, tracked layout — so the
    planner decomposes against the real repo, never the control-plane repo that
    host-side claude was launched from (triage F6, planner sibling of the #227
    wrong-codebase review)."""

    def _git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    repo = tmp_path / "closeloop"
    repo.mkdir()
    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    _git("remote", "add", "origin",
         "https://github.com/dsdevq/closeloop-bench-2026-07-13.git")
    (repo / "global.json").write_text('{"sdk":{"version":"9.0.315"}}\n')
    (repo / "backend").mkdir()
    (repo / "backend" / "Program.cs").write_text("// entry\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "angular.json").write_text("{}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")

    seen: dict = {}
    out = await plan_goal("add CI", str(repo), _capture_prompt(seen))
    assert len(out) == 1

    p = seen["prompt"]
    # match the injected section HEADING, not the prompt's own grounding
    # instruction (which always mentions "REPOSITORY CONTEXT").
    assert "REPOSITORY CONTEXT (facts" in p
    assert "closeloop-bench-2026-07-13.git" in p   # the ACTUAL repo, not devclaw
    assert "global.json: file" in p                # .NET marker probed on disk
    assert "pyproject.toml: missing" in p          # and it is NOT a python repo
    assert "tracked_top_level:" in p and "backend" in p


async def test_plan_prompt_distinguishes_empty_scaffold_target(tmp_path):
    """An empty directory renders a visibly-empty snapshot — the prompt is no
    longer byte-invariant between a populated repo and a bare scaffold target,
    so the model has grounds to decide whether a scaffold task belongs."""
    empty = tmp_path / "bare"
    empty.mkdir()

    seen: dict = {}
    await plan_goal("build a web app", str(empty), _capture_prompt(seen))
    p = seen["prompt"]
    assert "REPOSITORY CONTEXT (facts" in p
    assert "global.json: missing" in p and "package.json: missing" in p
    assert "tracked_top_level:" not in p          # nothing tracked — visibly empty

    # A not-yet-created workspace shows the "(not present)" marker — the
    # snapshot output that legitimately licenses a scaffold task.
    seen2: dict = {}
    await plan_goal("build a web app", str(tmp_path / "ghost"), _capture_prompt(seen2))
    assert "(not present)" in seen2["prompt"]


def test_plan_goal_prompt_carries_grounding_prohibition():
    """The rendered plan-goal prompt forbids inferring repo facts from the
    planner process's cwd/host context: facts come only from REPOSITORY
    CONTEXT, absent means unknown, and scaffold tasks are licensed only by an
    empty/not-present snapshot."""
    p = build_planner_prompt("goal", "/ws")
    assert "Do NOT infer repository facts" in p
    assert "unknown rather than substituting another codebase" in p
    assert "must not name a different language, framework" in p
    assert "empty or not-present workspace" in p


# ---- per-task acceptance criteria + constraints (task-brief structure) ------


def test_plan_goal_prompt_asks_for_grounded_acceptance_criteria_and_constraints():
    """Each task's `goal` must be briefed with acceptance criteria (OUTCOMES,
    not a recipe) + optional constraints, grounded in the goal + REPOSITORY
    CONTEXT and never invented — the same discipline as the repo-fact rule."""
    p = build_planner_prompt("goal", "/ws")
    assert "Acceptance criteria:" in p
    assert "Constraints:" in p
    assert "OUTCOMES" in p and "step-by-step recipe" in p
    # grounded in the goal + REPOSITORY CONTEXT, never invented (newline-agnostic)
    flat = " ".join(p.split())
    assert "Do NOT invent a criterion or constraint that neither supports" in flat
    assert "Ground every criterion and constraint in the goal and the REPOSITORY CONTEXT" in flat


def test_validate_plan_carries_a_multiline_criteria_goal_verbatim():
    """Shape B: acceptance criteria + constraints ride INSIDE the `goal` string
    as delineated sections. validate_plan needs no schema change — it preserves
    the multi-section goal byte-for-byte so it reaches the worker brief intact."""
    goal_body = (
        "Add a /health endpoint.\n\n"
        "Acceptance criteria:\n"
        "- GET /health returns 200 with {status: ok}\n"
        "- existing tests still pass\n\n"
        "Constraints:\n- do not touch the auth middleware"
    )
    out = validate_plan(_plan({"key": "t1", "goal": goal_body, "kind": "implement_feature"}))
    assert out[0].goal == goal_body  # carried through unchanged


def test_validate_plan_still_accepts_a_plain_goal_without_criteria():
    """Blank-safe: a task whose `goal` is a bare sentence (no criteria sections)
    validates exactly as before — the structure is additive, never required."""
    out = validate_plan(_plan({"key": "t1", "goal": "fix the typo"}))
    assert len(out) == 1 and out[0].goal == "fix the typo"


async def test_plan_goal_snapshot_is_best_effort(monkeypatch):
    """A crashing snapshot collector degrades to an ungrounded prompt — it can
    never fail planning (same best-effort contract as task_git's helpers)."""

    async def boom(_workspace_dir):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(planner, "_plan_repo_context", boom)

    seen: dict = {}
    out = await plan_goal("goal", "/ws", _capture_prompt(seen))
    assert len(out) == 1 and out[0].goal == "do it"
    assert "REPOSITORY CONTEXT (facts" not in seen["prompt"]  # degraded, not fabricated
