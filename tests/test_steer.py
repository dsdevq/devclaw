"""Steer inbox tests — inject direction into a running build (step 5)."""

import asyncio
import json

import pytest

from devclaw.engine import EngineRequest
from devclaw.planner import PlannedTask
from devclaw.project_service import ProjectService
from devclaw.project_store import ProjectStore
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


# ---- store: only pending tasks get the note ----


def test_append_note_touches_only_pending(store):
    store.create_program(id="p1", goal="g", workspace_dir="/ws")
    store.create_task(id="a", kind="implement_feature", workspace_dir="/ws", goal="task A", program_id="p1")
    store.create_task(id="b", kind="implement_feature", workspace_dir="/ws", goal="task B", program_id="p1")
    store.claim_pending("a")  # a → running; b stays pending

    ids = store.append_note_to_pending_tasks("p1", " :: NOTE")
    assert ids == ["b"]
    assert store.get_task("b").goal.endswith(" :: NOTE")
    assert store.get_task("a").goal == "task A"  # running task untouched


# ---- service: steer a live build ----


async def test_steer_injects_into_pending_tasks(store, tmp_path):
    gate = asyncio.Event()

    async def gated(req: EngineRequest):
        await gate.wait()
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": "ok"}

    async def grill(prompt):
        if "INTERVIEW SO FAR" in prompt:
            return json.dumps({"action": "done", "spec": "# spec\n## Milestones\n- M1"})
        return json.dumps({"action": "ask", "question": "q?", "recommended": "r"})

    async def spec_planner(spec, workspace_dir):
        return [
            PlannedTask(key="a", goal="first", kind="implement_feature", depends_on_keys=[], milestone="M1"),
            PlannedTask(key="b", goal="second", kind="implement_feature", depends_on_keys=["a"], milestone="M1"),
        ]

    queue = TaskQueue(store, runner=gated)
    svc = ProjectService(ProjectStore(str(tmp_path / "state")), queue, grill_caller=grill, spec_planner=spec_planner)

    r = await svc.start("build a thing", "/ws")
    pid = r["project_id"]
    await svc.answer(pid, "ok")        # → spec ready
    appr = await svc.approve(pid)      # → build starts; task "a" running (gated), "b" pending

    # steer mid-build → folds into the pending task only
    steered = await svc.steer(pid, "use Postgres, not SQLite")
    assert steered["status"] == "approved"
    assert steered["applied_to_pending_tasks"] == 1

    program_id = appr["program_id"]
    tasks = {t.goal.split("\n")[0]: t for t in store.list_program_tasks(program_id)}
    # the pending task ("second") carries the steer note; the running one ("first") doesn't
    pending = next(t for t in store.list_program_tasks(program_id) if t.status == "pending")
    assert "[STEER UPDATE]: use Postgres" in pending.goal
    running = next(t for t in store.list_program_tasks(program_id) if t.status == "running")
    assert "[STEER UPDATE]" not in running.goal

    # recorded in the steer log
    assert svc.get(pid).steer_log[0]["message"] == "use Postgres, not SQLite"

    gate.set()
    await queue.drain()
    assert store.get_program(program_id).status == "done"


async def test_steer_before_build_records_but_applies_nothing(store, tmp_path):
    async def grill(prompt):
        return json.dumps({"action": "ask", "question": "q?", "recommended": "r"})

    svc = ProjectService(ProjectStore(str(tmp_path / "state")), TaskQueue(store), grill_caller=grill)
    r = await svc.start("idea", "/ws")  # still eliciting, no program
    steered = await svc.steer(r["project_id"], "note while grilling")
    assert steered["applied_to_pending_tasks"] == 0
    assert svc.get(r["project_id"]).steer_log[0]["message"] == "note while grilling"


async def test_steer_unknown_project_raises(store, tmp_path):
    svc = ProjectService(ProjectStore(str(tmp_path / "state")), TaskQueue(store))
    with pytest.raises(KeyError):
        await svc.steer("nope", "x")
