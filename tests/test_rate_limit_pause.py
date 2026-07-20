"""Quota/rate-limit pause — a usage limit pauses dispatch + requeues, never
fails-and-retries (which would burn the remaining quota on the same doomed call).
Driven with stub runners (no docker)."""
from __future__ import annotations

import os
import subprocess

import pytest

from devclaw import task_queue
from devclaw.loom import limits  # the constant's real home post-extraction
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore, _now_ms
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


async def test_rate_limit_pauses_not_fails(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []

    async def rl(req: EngineRequest):
        calls.append(req.goal)
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "pending"          # requeued, NOT failed
    assert len(calls) == 1                # NOT retried — quota not burned
    until, reason = store.global_pause()
    assert until > _now_ms() and "rate_limit" in reason


async def test_real_error_still_fails(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    async def boom(req: EngineRequest):
        return {"status": "error", "error": "ModuleNotFoundError: No module named 'fastapi'"}

    q = TaskQueue(store, runner=boom)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    assert store.get_task(tid).status == "failed"  # real bug → fails as before
    assert store.global_pause()[0] == 0            # no pause set


async def test_pump_holds_dispatch_while_paused(store):
    called: list = []

    async def ok(req: EngineRequest):
        called.append(1)
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    store.set_global_pause(_now_ms() + 60_000, "manual")  # paused 60s out
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    assert store.get_task(tid).status == "pending"  # dispatch held
    assert called == []


async def test_resumes_after_pause_expires(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    state = {"n": 0}

    async def rl_then_ok(req: EngineRequest):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": "error", "error": "rate limit exceeded"}
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=rl_then_ok)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    assert store.get_task(tid).status == "pending"      # paused after the rl hit

    # simulate the pause window elapsing, then re-pump
    store.set_global_pause(_now_ms() - 1000, "expired")  # in the past
    q._pump()
    await q.drain()

    assert store.get_task(tid).status == "done"          # auto-resumed + completed
    assert state["n"] == 2
    assert store.global_pause()[0] == 0                   # pause cleared on resume


async def test_stated_hint_survives_not_clobbered_to_max(store, monkeypatch):
    """A STATED reset hint ("try again in 10 hours") must be honoured. The old
    policy clamped it to RATE_LIMIT_MAX_PAUSE_S (3600s) — devclaw then re-probed
    a multi-hour cap hourly, each probe a doomed dispatch."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "usage limit — try again in 10 hours"}

    q = TaskQueue(store, runner=rl)
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    until, _ = store.global_pause()
    assert until >= _now_ms() + 35_990_000            # ~10h out, NOT capped to 1h
    assert until <= _now_ms() + 36_010_000


async def test_stated_hint_caps_to_stated_max(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    monkeypatch.setattr(limits, "RATE_LIMIT_STATED_MAX_S", 60)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "usage limit — try again in 10 hours"}

    q = TaskQueue(store, runner=rl)
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    until, _ = store.global_pause()
    # even a stated hint has a ceiling — here shrunk to 60s
    assert until <= _now_ms() + 60_000 + 2_000


async def test_unstated_default_pause_unchanged(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    until, _ = store.global_pause()
    # no hint stated → the legacy default backoff, not the generous stated cap
    assert until <= _now_ms() + limits.RATE_LIMIT_PAUSE_S * 1000 + 2_000


async def test_absolute_reset_time_reaches_the_pause(store, monkeypatch):
    """The queue passes now_utc to the classifier, so Claude's ABSOLUTE reset
    wording ("resets 10pm (UTC)") becomes a real multi-hour pause instead of
    falling back to the 1800s default. Clock frozen at 18:00 UTC → 4h + 120s."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    from datetime import datetime, timezone

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 10, 18, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(task_queue, "datetime", _FrozenDatetime)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "Internal error: You're out of extra usage · resets 10pm (UTC)"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()

    assert store.get_task(tid).status == "pending"   # requeued, not failed
    until, reason = store.global_pause()
    expect = (4 * 3600 + 120) * 1000                 # seconds to 22:00 + slack
    assert _now_ms() + expect - 5_000 <= until <= _now_ms() + expect + 5_000
    assert "quota" in reason


# ---- bounded pause-requeues -------------------------------------------------
# A permanently-failing task whose error text happens to match the quota/rate
# regexes must not loop pause→requeue→re-run forever: the workspace breaker
# only counts `failed` rows, and a paused task never becomes one.


async def test_pause_requeue_is_bounded(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    monkeypatch.setattr(task_queue, "MAX_PAUSE_REQUEUES", 2)
    monkeypatch.setattr(task_queue, "WORKSPACE_BREAK_THRESHOLD", 1)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")

    await q.drain()
    t = store.get_task(tid)
    assert t.status == "pending" and t.pause_count == 1   # counted per requeue

    store.set_global_pause(_now_ms() - 1000, "expired")   # window elapses → re-run
    q._pump()
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "pending" and t.pause_count == 2

    store.set_global_pause(_now_ms() - 1000, "expired")
    q._pump()
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"                           # bound reached → failed, not requeued
    assert "exceeded 2 usage-limit pauses" in t.error
    assert "429" in t.error                               # the real reason is preserved
    until, _ = store.global_pause()
    assert until > _now_ms()                              # the account IS limited — pause still set
    # …and the breaker finally SEES the failure (threshold 1 → tripped)
    assert store.get_workspace_break("/ws")[0] > _now_ms()


async def test_crash_recovery_requeue_does_not_count_as_pause(store):
    """reset_running_to_pending (startup crash recovery) must NOT eat into the
    pause-requeue budget — only quota-pause requeues increment pause_count."""
    tid = "t1"
    store.create_task(id=tid, kind="implement_feature", workspace_dir="/ws", goal="g")
    store.mark_running(tid)
    store.reset_running_to_pending()
    assert store.get_task(tid).pause_count == 0


# ---- WIP preserved across a pause (T0.7) -------------------------------------
# The workspace survives a pause-requeue untouched, but the interrupted attempt's
# work is (1) invisible to the re-run — the pristine goal never mentions it — and
# (2) fragile as uncommitted tree state. So the pause path snapshots the dirty
# tree as a wip commit, and a re-run with pause_count > 0 gets an interruption
# brief telling the agent to continue, not restart.


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _git_repo(tmp_path):
    """A real tmp git repo with one committed file — the pattern
    tests/test_integrity_gate.py builds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("x = 1\n")
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _git_out(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


async def test_pause_snapshots_dirty_tree_as_wip_commit(store, tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    repo = _git_repo(tmp_path)

    async def rl(req: EngineRequest):
        # the agent got half-way before the limit hit — dirty tree at requeue time
        with open(os.path.join(req.workspace_dir, "half_done.py"), "w") as fh:
            fh.write("partial = True\n")
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir=str(repo), goal="g")
    await q.drain()

    assert store.get_task(tid).status == "pending"        # requeued as before
    head = _git_out(repo, "log", "--oneline", "-1")
    assert "wip(devclaw): interrupted" in head            # snapshot on the current branch
    assert tid[:8] in head
    assert _git_out(repo, "status", "--porcelain").strip() == ""  # tree clean — work durable


async def test_rerun_after_pause_gets_interruption_brief(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    goals: list = []

    async def rl_then_ok(req: EngineRequest):
        goals.append(req.goal)
        if len(goals) == 1:
            return {"status": "error", "error": "rate limit exceeded"}
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=rl_then_ok)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="build the thing")
    await q.drain()
    assert store.get_task(tid).status == "pending"

    store.set_global_pause(_now_ms() - 1000, "expired")   # window elapses → re-run
    q._pump()
    await q.drain()

    assert store.get_task(tid).status == "done"
    assert goals[0] == "build the thing"                  # pause_count 0 → pristine goal
    assert "[Resuming after a usage-limit interruption (pause 1)]" in goals[1]
    # The brief must NOT promise the partial progress is there — a failed retry's
    # rewind to the persisted base may have wiped it; the agent is told to check.
    assert "CONTINUE from whatever state is actually there" in goals[1]
    assert "build the thing" in goals[1]                  # the original goal still follows


async def test_pause_with_non_git_workspace_still_requeues(store, tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi\n")

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir=str(ws), goal="g")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "pending" and t.pause_count == 1   # pause path unaffected
    assert store.global_pause()[0] > _now_ms()
    assert not (ws / ".git").exists()                     # no stray commit artifacts


async def test_snapshot_crash_never_blocks_the_pause(store, tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    def boom(host_dir, task_id):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(task_queue, "_wip_snapshot_sync", boom)

    async def rl(req: EngineRequest):
        return {"status": "error", "error": "API Error: 429 Too Many Requests"}

    q = TaskQueue(store, runner=rl)
    tid = q.submit(kind="implement_feature", workspace_dir=str(tmp_path), goal="g")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "pending" and t.pause_count == 1   # pause path completed
    assert store.global_pause()[0] > _now_ms()            # pause still set


# ---- gate baseline survives the pause-requeue -------------------------------
# The pause path lands a wip snapshot COMMIT on the branch, so by the resumed
# run HEAD is the half-done work itself. The baseline the gates diff against is
# captured once per TASK and persisted on the row — re-capturing it at resume
# made the wip commit the base, and review rejected fully-present work as "no
# deliverable in the diff" (closeloop-bench b6d53bbd, 2026-07-19).


async def test_resumed_task_gate_baseline_is_original_base_not_wip_snapshot(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    repo = _git_repo(tmp_path)
    base_sha = _git_out(repo, "rev-parse", "HEAD").strip()

    diff_bases: list = []

    async def recording_diff(host_dir, base=""):
        diff_bases.append(base)
        return ""

    monkeypatch.setattr(task_queue, "_git_diff", recording_diff)

    runs: list = []

    async def rl_then_ok(req: EngineRequest):
        runs.append(req.goal)
        if len(runs) == 1:
            # half-way through when the limit hits — dirty tree at requeue time
            with open(os.path.join(req.workspace_dir, "half_done.py"), "w") as fh:
                fh.write("partial = True\n")
            return {"status": "error", "error": "API Error: 429 Too Many Requests"}
        return {"status": "ok", "workspaceDir": req.workspace_dir,
                "verify": {"ran": True, "cmd": "true", "passed": True,
                           "exit_code": 0, "timed_out": False, "output": ""}}

    q = TaskQueue(store, runner=rl_then_ok)
    tid = q.submit(kind="implement_feature", workspace_dir=str(repo), goal="g",
                   verify_cmd="true")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "pending"                       # requeued by the pause
    assert t.pre_run_sha == base_sha                   # baseline persisted at first run
    wip_head = _git_out(repo, "rev-parse", "HEAD").strip()
    assert wip_head != base_sha                        # wip snapshot moved HEAD

    store.set_global_pause(_now_ms() - 1000, "expired")  # window elapses → re-run
    q._pump()
    await q.drain()

    assert store.get_task(tid).status == "done"
    # the gates judged the resumed run against the ORIGINAL base, not the wip tip
    assert diff_bases and all(b == base_sha for b in diff_bases)


async def test_stale_persisted_baseline_degrades_to_fresh_capture(
    store, tmp_path, monkeypatch
):
    # Best-effort contract: a persisted sha that no longer resolves (workspace
    # re-cloned meanwhile) must not wedge the run — fall back to a fresh
    # capture and overwrite the stale row value.
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    repo = _git_repo(tmp_path)
    head_sha = _git_out(repo, "rev-parse", "HEAD").strip()

    async def ok(req: EngineRequest):
        return {"status": "ok", "workspaceDir": req.workspace_dir,
                "verify": {"ran": True, "cmd": "true", "passed": True,
                           "exit_code": 0, "timed_out": False, "output": ""}}

    q = TaskQueue(store, runner=ok)
    tid = q.submit(kind="implement_feature", workspace_dir=str(repo), goal="g",
                   verify_cmd="true")
    store.set_task_pre_run_sha(tid, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "done"                          # never wedged
    assert t.pre_run_sha == head_sha                   # stale value overwritten


# ---- hold legibility at the pump gate (2026-07-20 silent window-hold) ------

def _closed_window_now() -> tuple[str, str]:
    """An enabled window guaranteed CLOSED at the current wall-clock (opens 3h
    from now, 1h wide) — keeps these tests green at any time of day."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    return (now + timedelta(hours=3)).strftime("%H:%M"), (now + timedelta(hours=4)).strftime("%H:%M")


async def test_pause_expiring_into_closed_window_logs_held_not_resuming(store, capsys):
    """Named regression (2026-07-20): a quota pause whose reset lands OUTSIDE
    the run window must not announce "resuming" — the old gate order printed it,
    then operator_block silently held dispatch for hours, which read as a missed
    auto-resume. The expired pause still clears; the hold is named instead."""
    async def ok(req: EngineRequest):
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    start, end = _closed_window_now()
    store.set_run_schedule(True, start, end, "UTC")
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    store.set_global_pause(_now_ms() - 1000, "quota: out of extra usage - resets 6am (UTC)")
    q._pump()
    await q.drain()

    err = capsys.readouterr().err
    assert "resuming" not in err                       # the lie is gone
    assert "quota pause expired" in err and "dispatch held" in err
    assert store.global_pause()[0] == 0                # expired pause still cleared
    assert store.get_task(tid).status == "pending"     # held by the window, legibly


async def test_window_hold_logs_once_not_every_tick(store, capsys):
    """A persistent hold names itself ONCE, not once per 10s tick."""
    async def ok(req: EngineRequest):
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    start, end = _closed_window_now()
    store.set_run_schedule(True, start, end, "UTC")
    q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    q._pump()
    q._pump()
    q._pump()
    await q.drain()

    assert capsys.readouterr().err.count("dispatch held") == 1


async def test_window_reopen_logs_hold_lifted_and_dispatches(store, capsys):
    """When the window opens again the queue says so and dispatch flows."""
    async def ok(req: EngineRequest):
        return {"status": "ok", "workspaceDir": req.workspace_dir}

    q = TaskQueue(store, runner=ok)
    start, end = _closed_window_now()
    store.set_run_schedule(True, start, end, "UTC")
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g")
    await q.drain()
    assert store.get_task(tid).status == "pending"     # window held it

    store.clear_run_schedule()
    q._pump()
    await q.drain()

    assert "dispatch hold lifted" in capsys.readouterr().err
    assert store.get_task(tid).status == "done"
