"""TaskQueue integration tests with stub planner + runner (no docker, no claude).

Exercises the async orchestration: standalone tasks, program DAGs in dep order,
sticky failure, and event recording — the logic the old test:dag harness covered.
"""

import pytest

from devclaw.planner import PlannedTask
from devclaw.sandcastle_runner import OpenHandsRequest, RunnerEvent
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_runner(record: list[str]):
    async def runner(req: OpenHandsRequest):
        record.append(req.goal)
        if req.on_event:
            req.on_event(RunnerEvent(id="1", type="ActionEvent", source="agent", ts=0, payload={"g": req.goal}))
        return {"status": "ok", "workspaceDir": req.workspace_dir, "message": f"did: {req.goal}"}
    return runner


async def test_standalone_task_runs_and_settles(store):
    seen: list[str] = []
    q = TaskQueue(store, runner=_ok_runner(seen))
    task_id = q.submit(kind="implement_feature", workspace_dir="/ws", goal="hello")
    await q.drain()
    t = store.get_task(task_id)
    assert t.status == "done"
    assert seen == ["hello"]
    # the stub runner emitted one event
    evs = store.list_events(task_id=task_id)
    assert len(evs) == 1 and evs[0].type == "ActionEvent"


async def test_program_dag_runs_in_dependency_order(store):
    seen: list[str] = []

    async def planner(goal, workspace_dir):
        return [
            PlannedTask(key="b", goal="second", kind="implement_feature", depends_on_keys=["a"]),
            PlannedTask(key="a", goal="first", kind="implement_feature", depends_on_keys=[]),
        ]

    q = TaskQueue(store, planner=planner, runner=_ok_runner(seen))
    program_id = q.submit_program(workspace_dir="/ws", goal="big goal")
    await q.drain()

    program = store.get_program(program_id)
    assert program.status == "done"
    # "first" must execute before "second"
    assert seen == ["first", "second"]
    tasks = store.list_program_tasks(program_id)
    assert all(t.status == "done" for t in tasks)


async def test_program_planner_failure_marks_failed(store):
    async def planner(goal, workspace_dir):
        raise RuntimeError("planner exploded")

    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    assert store.get_program(program_id).status == "failed"


async def test_task_failure_propagates_to_program(store):
    async def failing_runner(req: OpenHandsRequest):
        return {"status": "error", "error": "kaboom"}

    async def planner(goal, workspace_dir):
        return [PlannedTask(key="a", goal="g", kind="implement_feature", depends_on_keys=[])]

    q = TaskQueue(store, planner=planner, runner=failing_runner)
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    p = store.get_program(program_id)
    assert p.status == "failed"
