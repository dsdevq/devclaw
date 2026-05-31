"""Elicitation grill tests — step validation, next_step, and the full
grill → spec → approve → execute flow with stubbed cognition + engine."""

import json

import pytest

from devclaw.elicitation import next_step, validate_step
from devclaw.engine import EngineRequest
from devclaw.planner import PlannedTask, PlannerError
from devclaw.project_service import ProjectService
from devclaw.project_store import ProjectStore
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


# ---- validate_step ----


def test_validate_ask_step():
    s = validate_step({"action": "ask", "question": "What stack?", "recommended": "Next.js"})
    assert s == {"action": "ask", "question": "What stack?", "recommended": "Next.js"}


def test_validate_done_step():
    s = validate_step({"action": "done", "spec": "# spec\n## Goal\nx"})
    assert s["action"] == "done" and s["spec"].startswith("# spec")


def test_validate_ask_without_question_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "ask", "recommended": "x"})


def test_validate_done_without_spec_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "done"})


def test_validate_unknown_action_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "build_it_now"})


# ---- next_step ----


async def test_next_step_asks_then_finalizes():
    async def ask_stub(_prompt):
        return json.dumps({"action": "ask", "question": "Who is it for?", "recommended": "devs"})

    step = await next_step("a CLI tool", [], ask_stub)
    assert step["action"] == "ask" and step["question"] == "Who is it for?"

    async def done_stub(_prompt):
        return '```json\n{"action":"done","spec":"# spec\\n## Goal\\nship it"}\n```'

    step = await next_step("a CLI tool", [{"question": "q", "recommended": "r", "answer": "a"}], done_stub)
    assert step["action"] == "done" and "ship it" in step["spec"]


# ---- full flow: grill → spec → approve → execute ----


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


async def test_full_build_from_scratch_flow(store, tmp_path):
    # Scripted grill: ask 2 questions, then finalize a spec.
    turns = {"n": 0}

    async def grill(_prompt):
        turns["n"] += 1
        if turns["n"] == 1:
            return json.dumps({"action": "ask", "question": "Q1 stack?", "recommended": "Python"})
        if turns["n"] == 2:
            return json.dumps({"action": "ask", "question": "Q2 db?", "recommended": "sqlite"})
        return json.dumps({"action": "done", "spec": "# todo — spec\n## Milestones\n- M1"})

    # Stub spec planner → a 2-task milestoned DAG.
    async def spec_planner(spec, workspace_dir):
        assert "spec" in spec
        return [
            PlannedTask(key="m1", goal="scaffold", kind="implement_feature", depends_on_keys=[], milestone="M1"),
            PlannedTask(key="m2", goal="logic", kind="implement_feature", depends_on_keys=["m1"], milestone="M1"),
        ]

    built: list[str] = []

    async def runner(req: EngineRequest):
        built.append(req.goal)
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "ok"}

    queue = TaskQueue(store, runner=runner)
    svc = ProjectService(ProjectStore(str(tmp_path / "state")), queue, grill_caller=grill, spec_planner=spec_planner)

    # 1. start → first question
    r = await svc.start("build a todo app", "/ws")
    pid = r["project_id"]
    assert r["status"] == "eliciting" and r["question"] == "Q1 stack?" and r["recommended"] == "Python"

    # 2. answer → second question
    r = await svc.answer(pid, "use Python")
    assert r["status"] == "eliciting" and r["question"] == "Q2 db?"

    # 3. answer → spec ready
    r = await svc.answer(pid, "sqlite is fine")
    assert r["status"] == "ready" and r["spec"].startswith("# todo")

    # the transcript captured both answered turns
    project = svc.get(pid)
    assert [t["answer"] for t in project.transcript] == ["use Python", "sqlite is fine"]
    assert project.pending_question is None

    # 4. approve → plans the spec and starts a program
    r = await svc.approve(pid)
    program_id = r["program_id"]
    assert r["status"] == "approved"

    # 5. the build runs to completion via the stub engine, in dep order
    await queue.drain()
    program = store.get_program(program_id)
    assert program.status == "done"
    assert built == ["scaffold", "logic"]
    # milestones survived spec → DAG → DB
    assert all(t.milestone == "M1" for t in store.list_program_tasks(program_id))

    # approve is idempotent
    again = await svc.approve(pid)
    assert again["program_id"] == program_id


async def test_answer_unknown_project_raises(store, tmp_path):
    svc = ProjectService(ProjectStore(str(tmp_path / "state")), TaskQueue(store))
    with pytest.raises(KeyError):
        await svc.answer("nope", "x")


async def test_approve_before_ready_raises(store, tmp_path):
    async def grill(_prompt):
        return json.dumps({"action": "ask", "question": "q?", "recommended": "r"})

    svc = ProjectService(ProjectStore(str(tmp_path / "state")), TaskQueue(store), grill_caller=grill)
    r = await svc.start("idea", "/ws")
    with pytest.raises(ValueError):
        await svc.approve(r["project_id"])  # still eliciting, no spec
