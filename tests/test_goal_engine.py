"""In-process engine adapter — dispatch routes into the real queue; poll reads
real rows. This is the seam that replaced goalclaw's HTTP MCP client; the whole
point is that there's no wire, so we test against a real StateStore + TaskQueue
driven by a stub runner."""

from __future__ import annotations

import json
import subprocess

import pytest

from devclaw.engine import EngineRequest
from devclaw.goal.engine import InProcessEngine, _gate_passed, _task_detail
from devclaw.goal.models import Action, Goal
from devclaw.planner import PlannedTask
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


def _goal(workspace_dir: str = "/ws"):
    return Goal(
        id="g", objective="obj", cadence="1d", engine="devclaw",
        workspace_dir=workspace_dir, verify_cmd="pytest -q", backlog=["a"],
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
    # PR7: dispatch() no longer auto-pumps (submit(pump=False)) — it only
    # creates the row, so the goal tick can wrap it in an atomic
    # transaction. Direct-dispatch callers (like this test) now kick the
    # queue explicitly, same as the goal tick does right after its dispatch
    # transaction commits.
    engine.kick()
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
async def test_dispatch_threads_scaffold_flag_to_task_row(wired):
    """L3 (#222): a scaffold Action lands a task row with scaffold=True so the
    queue can skip the adversarial review gate for it."""
    engine, queue, store = wired
    action = Action(
        engine="devclaw", tool="implement_feature",
        goal="Scaffold an Angular workspace", open_pr=False, scaffold=True,
    )
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert store.get_task(ref.id).scaffold is True


@pytest.mark.asyncio
async def test_dispatch_default_action_is_not_scaffold(wired):
    engine, queue, store = wired
    action = Action(engine="devclaw", tool="implement_feature", goal="add /health", open_pr=False)
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert store.get_task(ref.id).scaffold is False


@pytest.mark.asyncio
async def test_planner_verify_cmd_no_longer_overrides_firmed_gate(wired):
    """F5 (mechanical hole): ``validate()`` used to accept an undocumented
    "verify_cmd" in the planner's JSON — a field the prompt schema never even
    offered — and dispatch then honored it over the FIRMED command
    (``action.verify_cmd or goal.verify_cmd``): an ungrounded guess mechanically
    replacing the repo-true gate. The field is now ignored, gracefully: the
    response still validates (never an error), and the dispatched task row
    carries the firmed command."""
    from devclaw.goal.planner import validate

    engine, queue, store = wired
    res = validate({
        "decision": "act",
        "note": "n",
        "actions": [{
            "tool": "implement_feature", "goal": "add /health",
            "open_pr": False, "verify_cmd": "pytest",   # the ungrounded guess
        }],
    })
    assert res.decision == "act"          # still validates — no error
    action = res.actions[0]
    assert action.verify_cmd is None      # the guess was not honored
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert store.get_task(ref.id).verify_cmd == "pytest -q"  # the firmed gate


@pytest.mark.asyncio
async def test_dispatch_review_is_never_scaffold(wired):
    """A read-only review_repository has no diff to review — even a mis-set
    scaffold flag on the Action is forced off on the row."""
    engine, queue, store = wired
    action = Action(engine="devclaw", tool="review_repository", goal="assess", scaffold=True)
    ref = await engine.dispatch(action, _goal(), notify_url="")
    assert store.get_task(ref.id).scaffold is False


@pytest.mark.asyncio
async def test_dispatch_program_then_poll(wired, tmp_path):
    engine, queue, store = wired
    # open_pr=True → the program's children inherit deliver=True, so they need a
    # real git workspace (prepare_workspace guarantees one in production; a
    # broken delivery now settles the task 'failed' instead of a silent
    # done-without-PR). The stub runner writes nothing, so delivery is the
    # benign "no changes to deliver" and the program settles done.
    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    action = Action(engine="devclaw", tool="start_program", goal="build the thing", open_pr=True)
    ref = await engine.dispatch(action, _goal(str(repo)), notify_url="")
    assert ref.ref_kind == "program"
    # PR7: dispatch() no longer auto-kicks off planning (submit_program(pump=
    # False)) — see the note in test_dispatch_feature_then_poll_terminal above.
    engine.kick()
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


def test_task_detail_keeps_full_agent_output_for_review_repository():
    """The done-gate evaluator judges the goal against the agent's report —
    truncating ``agent_output`` at 6 KB kept only the SDK's user-message panel
    echoing the brief (which contains ``<clause 1 text>`` placeholders in its
    format spec) plus a handful of early `status=pending` tool calls; the
    actual filled per-clause section lives at the END of a 60–160 KB
    transcript. Reviews must preserve the full output."""
    big_review = (
        "Message from User panel\n"
        "## Per-clause evidence\n"
        "1. <clause 1 text>\n"
        "   satisfied: yes | no | partial\n"
    ) + ("ACP Tool Call decoration ls -la /workspace status=pending\n" * 600) + (
        "## Per-clause evidence\n"
        "1. Health endpoint exists\n"
        "   satisfied: yes\n"
        "   evidence: app/routes.py:42 health_handler covered by tests/test_health.py:8\n"
        "## Summary\nAll satisfied.\n"
    )
    assert len(big_review) > 30_000  # confirm we're testing the truncation regime
    result = json.dumps({"status": "ok", "agent_output": big_review})
    detail = _task_detail("review_repository", result, None, None)
    # the filled clause-evidence line — buried in the 30 KB+ transcript — must reach the evaluator
    assert "app/routes.py:42 health_handler" in detail
    assert "All satisfied." in detail


def test_task_detail_still_truncates_other_kinds():
    """For implement_feature / fix_bug, the ``agent_output`` is a work summary
    that gets written to deliveries.md and fed to the planner. 6 KB is plenty
    — bloating deliveries.md with full transcripts would hurt planner context."""
    big_summary = "x" * 50_000
    result = json.dumps({"status": "ok", "agent_output": big_summary})
    detail = _task_detail("implement_feature", result, None, None)
    assert len(detail) < 10_000  # bounded
