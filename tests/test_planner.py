"""Planner unit tests — extract_json, order_tasks (topo/cycles/refs), and the
decomposer-routed program planning spine (planned_from_checklist + plan_program,
ADR 0003 stage 1)."""

import json
import subprocess

import pytest

from devclaw import planner
from devclaw.goal.models import Checklist, ChecklistItem
from devclaw.planner import (
    PlannedTask,
    PlannerError,
    extract_json,
    order_tasks,
    plan_program,
    planned_from_checklist,
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


# ---- order_tasks (DAG shape validation, extracted from the old validate_plan) ----


def _t(key, deps=(), **kw):
    return PlannedTask(key=key, goal="g", kind="implement_feature",
                       depends_on_keys=list(deps), **kw)


def test_single_task_no_deps():
    out = order_tasks([_t("t1")])
    assert len(out) == 1 and out[0].key == "t1"


def test_linear_chain_orders_topologically():
    out = order_tasks([_t("c", ["b"]), _t("b", ["a"]), _t("a")])
    assert [t.key for t in out] == ["a", "b", "c"]


def test_diamond_dag_ordered():
    out = order_tasks([_t("d", ["b", "c"]), _t("b", ["a"]), _t("c", ["a"]), _t("a")])
    order = [t.key for t in out]
    assert order[0] == "a" and order[-1] == "d"
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_cycle_rejected():
    with pytest.raises(PlannerError, match="cycle"):
        order_tasks([_t("a", ["b"]), _t("b", ["a"])])


def test_self_dep_rejected():
    with pytest.raises(PlannerError, match="depends on itself"):
        order_tasks([_t("a", ["a"])])


def test_dangling_ref_rejected():
    with pytest.raises(PlannerError, match="unknown key"):
        order_tasks([_t("a", ["ghost"])])


def test_duplicate_key_rejected():
    with pytest.raises(PlannerError, match="Duplicate"):
        order_tasks([_t("a"), _t("a")])


# ---- planned_from_checklist (the decomposer → queue adapter) ----


def _item(id_, requirement="do the work", evidence="the work exists", **kw):
    return ChecklistItem(id=id_, requirement=requirement, evidence_target=evidence, **kw)


def test_program_planning_routes_through_the_decomposer_checklist():
    """The unification's near-1:1 map (ADR 0003 stage 1): id→key,
    requirement+evidence_target→goal, depends_on→depends_on_keys,
    milestone→milestone. Kind is always implement_feature — the requirement
    text, not the kind enum, directs the agent."""
    out = planned_from_checklist(Checklist(items=[
        _item("api", requirement="Add /health endpoint",
              evidence="GET /health returns 200", milestone="M2",
              depends_on=["scaffold"]),
        _item("scaffold", requirement="Scaffold the workspace",
              evidence="ng build passes", milestone="M1"),
    ]))
    assert [t.key for t in out] == ["scaffold", "api"]  # topo order
    api = next(t for t in out if t.key == "api")
    assert api.kind == "implement_feature"
    assert api.depends_on_keys == ["scaffold"]
    assert api.milestone == "M2"
    assert "Add /health endpoint" in api.goal
    assert "Evidence target" in api.goal and "GET /health returns 200" in api.goal


def test_planned_from_checklist_threads_scaffold_to_planned_task():
    """A ChecklistItem tagged scaffold must arrive at the queue with the flag
    intact — without the thread a program-path scaffold diff hits the
    adversarial review gate and fails closed on generator output."""
    out = planned_from_checklist(Checklist(items=[
        _item("gen", scaffold=True), _item("real", scaffold=False),
    ]))
    by_key = {t.key: t for t in out}
    assert by_key["gen"].scaffold is True
    assert by_key["real"].scaffold is False


def test_planner_note_rides_in_the_goal_brief():
    out = planned_from_checklist(Checklist(items=[
        _item("a", note="reuse the existing DbContext"),
    ]))
    assert "Planner note: reuse the existing DbContext" in out[0].goal


def test_empty_checklist_fails_planning():
    with pytest.raises(PlannerError, match="no plannable items"):
        planned_from_checklist(Checklist(items=[]))


def test_checklist_cycle_rejected_at_the_adapter():
    """validate_checklist prunes dangling/self deps but not multi-node cycles;
    a cycle that reached the queue would deadlock the DAG (no task ever
    ready), so the adapter's order_tasks pass must reject it."""
    with pytest.raises(PlannerError, match="cycle"):
        planned_from_checklist(Checklist(items=[
            _item("a", depends_on=["b"]), _item("b", depends_on=["a"]),
        ]))


# ---- plan_program (with stubbed claude) ----

_TWO_ITEM_YAML = """checklist:
  - id: scaffold
    requirement: Scaffold the workspace
    evidence_target: ng build passes
    milestone: M1
    scaffold: true
  - id: api
    requirement: Add /health endpoint
    evidence_target: GET /health returns 200
    depends_on: [scaffold]
    milestone: M2
"""


def _capture_prompt(seen: dict, reply: str = _TWO_ITEM_YAML):
    """A stub caller that records the wire prompt and returns a checklist."""

    async def stub(prompt):
        seen["prompt"] = prompt
        return reply

    return stub


async def test_plan_program_maps_decomposer_output_to_a_dag():
    out = await plan_program("goal", "/ws", _capture_prompt({}))
    assert [t.key for t in out] == ["scaffold", "api"]
    assert out[0].scaffold is True and out[1].scaffold is False


async def test_plan_program_bubbles_decomposer_failure_as_planner_error():
    """A non-checklist response fails planning with PlannerError so the queue's
    existing mark-program-failed + notify path handles it unchanged."""

    async def stub(_prompt):
        return "I could not produce a checklist, sorry."

    with pytest.raises(PlannerError):
        await plan_program("goal", "/ws", stub)


async def test_plan_program_prompt_is_the_decomposer_prompt():
    """The program path rides the SAME planning spine as durable goals — the
    wire prompt is the decomposer's (goal facts + schema contract), not the
    retired plan-goal JSON prompt."""
    seen: dict = {}
    await plan_program("build a web app", "/ws", _capture_prompt(seen))
    p = seen["prompt"]
    assert "objective: build a web app" in p
    assert "done_when:" in p
    assert "Return the YAML now." in p


# ---- plan_program grounding (triage F6 — the planner sibling of #227) ----


async def test_plan_program_prompt_includes_repo_context_from_actual_workspace(tmp_path):
    """The wire prompt carries a REPOSITORY CONTEXT snapshot collected from the
    task's ACTUAL workspace — remote, key-file probes, tracked layout — so the
    decomposer plans against the real repo, never the control-plane repo that
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
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")

    seen: dict = {}
    out = await plan_program("add CI", str(repo), _capture_prompt(seen))
    assert len(out) == 2

    p = seen["prompt"]
    # match the injected section HEADING, not any instruction text.
    assert "REPOSITORY CONTEXT (mechanical facts" in p
    assert "closeloop-bench-2026-07-13.git" in p   # the ACTUAL repo, not devclaw
    assert "global.json: file" in p                # .NET marker probed on disk
    assert "pyproject.toml: missing" in p          # and it is NOT a python repo


async def test_plan_program_snapshot_is_best_effort(monkeypatch):
    """A crashing snapshot collector degrades to an ungrounded prompt — it can
    never fail planning (same best-effort contract as task_git's helpers)."""

    async def boom(_workspace_dir):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(planner, "_plan_repo_context", boom)

    seen: dict = {}
    out = await plan_program("goal", "/ws", _capture_prompt(seen))
    assert len(out) == 2
    assert "REPOSITORY CONTEXT (mechanical facts" not in seen["prompt"]  # degraded, not fabricated


def test_runaway_decomposition_trips_the_program_task_brake():
    """The hard-brakes contract: a runaway decomposition (whole-app goal
    exploding into micro-items) must fail planning loudly with an actionable
    reason, not enqueue an unbounded fleet of sandboxed agent runs. This is a
    cost backstop (ADR 0003 §7), distinct from the retired 'aim for 1-6'
    prompt sizing guidance (§4) — a legitimate ~30-item plan passes."""
    from devclaw.planner import MAX_PROGRAM_TASKS

    ok = Checklist(items=[_item(f"t{i}") for i in range(MAX_PROGRAM_TASKS)])
    assert len(planned_from_checklist(ok)) == MAX_PROGRAM_TASKS

    runaway = Checklist(items=[_item(f"t{i}") for i in range(MAX_PROGRAM_TASKS + 1)])
    with pytest.raises(PlannerError, match="brake"):
        planned_from_checklist(runaway)


def test_failure_log_rides_in_the_redispatch_brief():
    """Cross-dispatch continuity (#288 on the one-shot path): a re-dispatched
    item's brief must carry its prior failures so the next worker doesn't
    re-discover a failed approach one attempt at a time."""
    out = planned_from_checklist(Checklist(items=[
        _item("a", failure_log=["attempt 1: settled failed · build broke"]),
    ]))
    assert "Prior attempts at this item FAILED" in out[0].goal
    assert "build broke" in out[0].goal
