"""The test-integrity guard wired into the gate: a change that deletes a test
must NOT settle 'done' even when the verify gate (exit code) passes."""
from __future__ import annotations

import os
import subprocess

import pytest

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo_with_test(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _passing_gate():
    return {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
            "timed_out": False, "output": ""}


async def test_integrity_scanner_crash_fails_closed(store, tmp_path, monkeypatch):
    """A crash INSIDE the integrity scanner must not silently approve the
    change (T0.2): the old `except → None` meant any scanner bug shipped a
    potentially test-gutting diff unscanned. The crash feeds the retry loop
    and, with retries exhausted, fails the task with the real error."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    repo = _repo_with_test(tmp_path)

    def boom(diff):
        raise RuntimeError("scanner exploded")
    monkeypatch.setattr(task_queue, "scan_diff", boom)

    async def runner(req: EngineRequest):
        with open(os.path.join(req.workspace_dir, "f.py"), "w") as f:
            f.write("x = 1\n")
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": _passing_gate()}

    q = TaskQueue(store, runner=runner)
    tid = q.submit(kind="fix_bug", workspace_dir=str(repo), goal="g", verify_cmd="pytest")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "failed"
    assert "test-integrity gate crashed" in (t.error or "")
    assert "scanner exploded" in (t.error or "")


async def test_deleting_a_test_fails_the_gate(store, tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)  # no retry → straight to failed
    repo = _repo_with_test(tmp_path)

    async def runner(req: EngineRequest):
        # the agent "passes" by deleting the failing test — gate exit code is 0
        os.remove(os.path.join(req.workspace_dir, "tests", "test_x.py"))
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": _passing_gate()}

    q = TaskQueue(store, runner=runner)
    tid = q.submit(kind="fix_bug", workspace_dir=str(repo), goal="g", verify_cmd="pytest")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "failed"                       # integrity caught it despite green gate
    assert "test" in (t.error or "").lower()


async def test_clean_change_still_passes(store, tmp_path):
    repo = _repo_with_test(tmp_path)

    async def runner(req: EngineRequest):
        # a legitimate change: add a new test, touch nothing existing
        with open(os.path.join(req.workspace_dir, "tests", "test_x.py"), "a") as fh:
            fh.write("\ndef test_y():\n    assert 1 == 1\n")
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": _passing_gate()}

    q = TaskQueue(store, runner=runner)
    tid = q.submit(kind="fix_bug", workspace_dir=str(repo), goal="g", verify_cmd="pytest")
    await q.drain()

    assert store.get_task(tid).status == "done"       # adding tests is fine
