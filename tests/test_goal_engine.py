"""In-process engine adapter — dispatch routes into the real queue; poll reads
real rows. This is the seam that replaced goalclaw's HTTP MCP client; the whole
point is that there's no wire, so we test against a real StateStore + TaskQueue
driven by a stub runner."""

from __future__ import annotations

import json

import pytest

from devclaw.engine import EngineRequest
from devclaw.goal_engine import InProcessEngine, _gate_passed, _task_detail
from devclaw.goal_models import Action, Goal
from devclaw.planner import PlannedTask
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


def _goal():
    return Goal(
        id="g", objective="obj", cadence="1d", engine="devclaw",
        workspace_dir="/ws", verify_cmd="pytest -q", backlog=["a"],
    )


async def _ok_runner(request: EngineRequest) -> dict:
    out = {"status": "ok", "message": f"did: {request.goal[:40]}"}
    if request.verify_cmd:
        out["verify"] = {"ran": True, "cmd": request.verify_cmd, "passed": True, "output": "1 passed"}
    return out


@pytest.fixture()
def wired(tmp_path):
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store, planner=lambda g, w: _stub_plan(g, w), runner=_ok_runner)
    engine = InProcessEngine(queue, store)
    yield engine, queue, store
    store.close()


async def _stub_plan(goal, workspace_dir):
    return [PlannedTask(key="t1", goal=goal, kind="implement_feature")]


@pytest.mark.asyncio
async def test_dispatch_feature_then_poll_terminal(wired):
    engine, queue, store = wired
    action = Action(engine="devclaw", tool="implement_feature", goal="add /health", open_pr=False)
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert ref.ref_kind == "task"
    await queue.drain()
    poll = await engine.poll(ref)
    assert poll.terminal is True
    assert poll.status == "done"
    assert poll.gate_passed is True            # read straight from result_json
    assert "did: add /health" in poll.detail   # richer than the old wire blob


@pytest.mark.asyncio
async def test_dispatch_review_is_readonly(wired):
    engine, queue, store = wired
    action = Action(engine="devclaw", tool="review_repository", goal="assess", open_pr=True)
    ref = await engine.dispatch(action, _goal(), notify_url="")
    # review must not carry a gate or a deliver flag even if open_pr was passed
    t = store.get_task(ref.id)
    assert t.kind == "review_repository"
    assert t.verify_cmd is None
    assert t.deliver is False


@pytest.mark.asyncio
async def test_dispatch_program_then_poll(wired):
    engine, queue, store = wired
    action = Action(engine="devclaw", tool="start_program", goal="build the thing", open_pr=True)
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert ref.ref_kind == "program"
    await queue.drain()
    poll = await engine.poll(ref)
    assert poll.status == "done"
    assert poll.terminal is True


def test_gate_passed_and_detail_helpers():
    rj = json.dumps({"status": "ok", "message": "hi", "verify": {"ran": True, "cmd": "pytest", "passed": False, "output": "1 failed"}})
    assert _gate_passed(rj) is False
    assert _gate_passed(None) is None
    detail = _task_detail("implement_feature", rj, error=None, pr_url="http://pr/1")
    assert "PR: http://pr/1" in detail
    assert "FAILED" in detail
    assert "1 failed" in detail


def test_task_detail_prefers_agent_output_over_envelope():
    """Regression: the discovery brief / evaluator must see the agent's real
    analysis (agent_output), not the generic 'OpenHands completed.' envelope.
    Surfaced by the 2026-06-07 live test, where the wrong field starved cognition."""
    result = json.dumps({
        "status": "ok", "message": "OpenHands completed.",
        "agent_output": "The repo is a bare scaffold: two functions, no persistence, no tests.",
    })
    detail = _task_detail("review_repository", result, None, None)
    assert "bare scaffold" in detail
    assert "OpenHands completed." not in detail
