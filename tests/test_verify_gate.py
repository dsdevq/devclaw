"""Verify-gate tests — the gate that stops devclaw trusting the agent's self-report.

Two halves:
  * execution  — `_run_verify` (in the in-sandbox runner) runs the command in the
    workspace and returns a verdict.
  * interpretation — `TaskQueue._run_and_settle` marks the task done ONLY when the
    gate passed (or no gate); a failed/timed-out gate fails the task with output.

No docker, no claude: the runner helper runs real trivial shell commands; the
settle logic is driven with a stub runner that returns a verdict.
"""

import importlib.util
from pathlib import Path

import pytest

from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue, _verify_failure_summary

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_verify", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


# ---- execution: _run_verify (real subprocesses) ----------------------------


def test_run_verify_passes_on_exit_zero(runner, tmp_path):
    v = runner._run_verify("true", str(tmp_path))
    assert v == {
        "ran": True, "cmd": "true", "passed": True,
        "exit_code": 0, "timed_out": False, "output": "",
    }


def test_run_verify_fails_on_nonzero(runner, tmp_path):
    v = runner._run_verify("exit 7", str(tmp_path))
    assert v["passed"] is False and v["exit_code"] == 7 and v["timed_out"] is False


def test_run_verify_runs_in_the_workspace_dir(runner, tmp_path):
    (tmp_path / "marker.txt").write_text("WORKSPACE-MARKER")
    v = runner._run_verify("cat marker.txt", str(tmp_path))
    assert v["passed"] is True
    assert "WORKSPACE-MARKER" in v["output"]  # proves cwd == workspace


def test_run_verify_captures_output_on_failure(runner, tmp_path):
    v = runner._run_verify("echo boom-out; echo boom-err 1>&2; false", str(tmp_path))
    assert v["passed"] is False
    assert "boom-out" in v["output"] and "boom-err" in v["output"]


def test_run_verify_times_out_without_raising(runner, tmp_path):
    v = runner._run_verify("sleep 5", str(tmp_path), timeout=1)
    assert v["timed_out"] is True and v["passed"] is False and v["exit_code"] is None


# ---- interpretation: TaskQueue settle gate ---------------------------------


def _verdict_runner(verify: dict | None, captured: dict):
    """A stub engine that records the verify_cmd it was handed and returns an
    agent-ok result carrying the given gate verdict (None → no gate ran)."""
    async def runner(req: EngineRequest):
        captured["verify_cmd"] = req.verify_cmd
        result = {"status": "ok", "workspaceDir": req.workspace_dir, "message": "did it"}
        if verify is not None:
            result["verify"] = verify
        return result
    return runner


async def test_gate_pass_marks_done(store):
    cap: dict = {}
    v = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0, "timed_out": False, "output": ""}
    q = TaskQueue(store, runner=_verdict_runner(v, cap))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert cap["verify_cmd"] == "pytest"  # the gate command reached the engine


async def test_gate_fail_marks_failed_with_output(store):
    cap: dict = {}
    v = {"ran": True, "cmd": "pytest", "passed": False, "exit_code": 1, "timed_out": False, "output": "2 failed"}
    q = TaskQueue(store, runner=_verdict_runner(v, cap))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "verify gate failed" in t.error and "2 failed" in t.error


async def test_gate_timeout_marks_failed(store):
    cap: dict = {}
    v = {"ran": True, "cmd": "pytest", "passed": False, "exit_code": None, "timed_out": True, "output": ""}
    q = TaskQueue(store, runner=_verdict_runner(v, cap))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed" and "timed out" in t.error


async def test_no_gate_is_backward_compatible(store):
    # No verify_cmd → no `verify` in the result → agent-ok means done, as before.
    cap: dict = {}
    q = TaskQueue(store, runner=_verdict_runner(None, cap))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert cap["verify_cmd"] is None


# ---- persistence + summary -------------------------------------------------


def test_verify_cmd_persists_on_the_task(store):
    store.create_task(id="x1", kind="implement_feature", workspace_dir="/ws",
                      goal="g", verify_cmd="dotnet test")
    assert store.get_task("x1").verify_cmd == "dotnet test"
    assert store.get_task("x1").to_dict()["verifyCmd"] == "dotnet test"


def test_verify_failure_summary_shapes():
    failed = _verify_failure_summary(
        {"cmd": "pytest", "passed": False, "exit_code": 1, "timed_out": False, "output": "boom"}
    )
    assert "verify gate failed (exit 1)" in failed and "pytest" in failed and "boom" in failed
    timed = _verify_failure_summary(
        {"cmd": "pytest", "passed": False, "exit_code": None, "timed_out": True, "output": ""}
    )
    assert "timed out" in timed
