"""Regression: the post-gate diff must include work the agent COMMITTED.

The commit coda asks the engineer to commit its work, and in goal-branch mode
those commits land directly on ``goal/<id>`` — by settle time the tree can be
clean. The gates (test-integrity scan + adversarial review) used to judge only
``git diff`` + ``git diff --cached``, so a fully-committed change reviewed as a
no-op and was sent back with "requested changes" (live-found 2026-07-11: three
closeloop-bench tasks in a row rejected on a diff of trend-file noise while the
real work sat committed on the goal branch).

These tests use REAL git repos (no fake-diff autouse fixture here on purpose)
to pin the contract end to end: pre-run HEAD is the diff baseline.
"""
import subprocess

import pytest

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue, _git_diff_sync, _git_head_sync


def _git(cwd, *args):
    p = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30
    )
    assert p.returncode == 0, f"git {args} failed: {p.stderr}"
    return p.stdout


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("# base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


# ------------------------- unit: _git_diff_sync -------------------------

def test_diff_with_base_includes_committed_work(tmp_path):
    ws = _init_repo(tmp_path / "ws")
    base = _git_head_sync(str(ws))
    (ws / "feature.py").write_text("print('shipped')\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "feat: shipped")

    legacy = _git_diff_sync(str(ws))
    based = _git_diff_sync(str(ws), base)
    assert "feature.py" not in legacy  # the old view is blind to the commit
    assert "feature.py" in based and "shipped" in based


def test_diff_with_bad_base_falls_back_to_uncommitted_view(tmp_path):
    ws = _init_repo(tmp_path / "ws")
    (ws / "README.md").write_text("# edited\n")
    out = _git_diff_sync(str(ws), "not-a-ref")
    assert "README.md" in out  # legacy view still delivered


# --------------------- integration: settle path ---------------------

@pytest.mark.asyncio
async def test_review_gate_sees_agent_committed_work(store, monkeypatch, tmp_path):
    """An agent that commits everything (clean tree at settle) must present the
    committed change to the reviewer — not an empty/noise diff."""
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", True)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    ws = _init_repo(tmp_path / "ws")

    async def committing_runner(req: EngineRequest):
        p = tmp_path / "ws" / "feature.py"
        p.write_text("print('shipped')\n")
        _git(tmp_path / "ws", "add", "-A")
        _git(tmp_path / "ws", "commit", "-q", "-m", "feat: shipped")
        gate = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                "timed_out": False, "output": ""}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}

    seen: dict = {}

    async def reviewer(*, goal, kind, diff, repo_context=None):
        seen["diff"] = diff
        return {"verdict": "approve", "summary": "ok", "issues": [], "blocking": []}

    q = TaskQueue(store, runner=committing_runner, reviewer=reviewer)
    tid = q.submit(
        kind="implement_feature", workspace_dir=str(ws), goal="ship feature",
        verify_cmd="pytest",
    )
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert "feature.py" in seen["diff"] and "shipped" in seen["diff"]
