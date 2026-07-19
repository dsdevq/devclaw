"""TaskQueue integration tests with stub planner + runner (no docker, no claude).

Exercises the async orchestration: standalone tasks, program DAGs in dep order,
sticky failure, and event recording — the logic the old test:dag harness covered.
"""

import pytest

from devclaw.engine import EngineEvent, EngineRequest
from devclaw.planner import PlannedTask
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_runner(record: list[str]):
    async def runner(req: EngineRequest):
        record.append(req.goal)
        if req.on_event:
            req.on_event(EngineEvent(id="1", type="ActionEvent", source="agent", ts=0, payload={"g": req.goal}))
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


async def test_program_default_open_pr_is_false_child_tasks_dont_deliver(store):
    """Legacy behavior: submit_program without open_pr → children skip delivery.
    This is the pre-2026-07-03 shape and must stay the default for programs
    that don't ask for the reviewable-slice contract."""
    async def planner(goal, workspace_dir):
        return [PlannedTask(key="a", goal="do it", kind="implement_feature",
                            depends_on_keys=[])]
    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    tasks = store.list_program_tasks(program_id)
    assert all(t.deliver is False for t in tasks)
    program = store.get_program(program_id)
    assert program.open_pr is False and program.verify_cmd is None


async def test_program_open_pr_and_verify_cmd_propagate_to_children(store):
    """Reviewable-slice contract (2026-07-03): when submit_program is called
    with open_pr=True and verify_cmd, EVERY implement_feature/fix_bug child
    task the decomposer creates inherits both. Closes the closeloop-mission-v2
    defect where the activity-timeline program pushed straight to main because
    the flags stopped at submit_program."""
    async def planner(goal, workspace_dir):
        return [
            PlannedTask(key="a", goal="first slice", kind="implement_feature",
                        depends_on_keys=[]),
            PlannedTask(key="b", goal="second slice", kind="implement_feature",
                        depends_on_keys=["a"]),
        ]
    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(
        workspace_dir="/ws", goal="mission-shaped program",
        open_pr=True, verify_cmd="bash scripts/verify.sh",
    )
    await q.drain()
    program = store.get_program(program_id)
    assert program.open_pr is True
    assert program.verify_cmd == "bash scripts/verify.sh"
    tasks = store.list_program_tasks(program_id)
    assert len(tasks) == 2
    for t in tasks:
        assert t.deliver is True, f"child {t.goal!r} didn't inherit open_pr"
        assert t.verify_cmd == "bash scripts/verify.sh"


async def test_program_review_repository_children_never_deliver_even_when_open_pr(store):
    """review_repository is inherently read-only — it writes a review report,
    no code changes to deliver. Even under open_pr=True inheritance, review
    tasks skip PR + gate. Mirrors the standalone-task carve-out at engine.py."""
    async def planner(goal, workspace_dir):
        return [
            PlannedTask(key="build", goal="build slice", kind="implement_feature",
                        depends_on_keys=[]),
            PlannedTask(key="audit", goal="audit the build", kind="review_repository",
                        depends_on_keys=["build"]),
        ]
    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(
        workspace_dir="/ws", goal="build then audit",
        open_pr=True, verify_cmd="pytest",
    )
    await q.drain()
    tasks = {t.goal: t for t in store.list_program_tasks(program_id)}
    assert tasks["build slice"].deliver is True
    assert tasks["build slice"].verify_cmd == "pytest"
    assert tasks["audit the build"].deliver is False
    assert tasks["audit the build"].verify_cmd is None


async def test_program_persists_milestones(store):
    async def planner(goal, workspace_dir):
        return [
            PlannedTask(key="a", goal="scaffold", kind="implement_feature", depends_on_keys=[], milestone="M1"),
            PlannedTask(key="b", goal="feature", kind="implement_feature", depends_on_keys=["a"], milestone="M2"),
        ]

    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    by_goal = {t.goal: t.milestone for t in store.list_program_tasks(program_id)}
    assert by_goal == {"scaffold": "M1", "feature": "M2"}


async def test_program_planner_failure_marks_failed(store):
    async def planner(goal, workspace_dir):
        raise RuntimeError("planner exploded")

    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    assert store.get_program(program_id).status == "failed"


async def test_task_failure_propagates_to_program(store):
    async def failing_runner(req: EngineRequest):
        return {"status": "error", "error": "kaboom"}

    async def planner(goal, workspace_dir):
        return [PlannedTask(key="a", goal="g", kind="implement_feature", depends_on_keys=[])]

    q = TaskQueue(store, planner=planner, runner=failing_runner)
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    p = store.get_program(program_id)
    assert p.status == "failed"


async def test_program_child_task_row_carries_scaffold_flag(store):
    """ADR 0003 stage 1 regression: a PlannedTask tagged scaffold (threaded
    from ChecklistItem.scaffold by the decomposer adapter) must land on the
    child task ROW — the review-gate skip reads row.scaffold at settle, so a
    dropped thread would fail-close the gate on generator diffs."""
    async def planner(goal, workspace_dir):
        return [
            PlannedTask(key="gen", goal="ng new app", kind="implement_feature",
                        depends_on_keys=[], scaffold=True),
            PlannedTask(key="real", goal="wire the endpoint", kind="implement_feature",
                        depends_on_keys=["gen"]),
        ]

    q = TaskQueue(store, planner=planner, runner=_ok_runner([]))
    program_id = q.submit_program(workspace_dir="/ws", goal="x")
    await q.drain()
    by_goal = {t.goal: bool(t.scaffold) for t in store.list_program_tasks(program_id)}
    assert by_goal == {"ng new app": True, "wire the endpoint": False}


async def test_program_default_planner_is_the_decomposer_adapter(store):
    """The queue's default _planner slot routes through plan_program (the
    decomposer spine), not the retired plan_goal."""
    from devclaw import task_queue as tq

    assert not hasattr(tq, "plan_goal")
    q = TaskQueue(store, runner=_ok_runner([]))
    # the default lambda closes over plan_program
    assert "plan_program" in q._planner.__code__.co_names
