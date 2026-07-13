"""Discovery synthesis is grounded in the ACTUAL goal workspace (triage F4,
2026-07-13 — sibling of PR #227's wrong-codebase review bug).

On a failed/empty repo review, the discovery-synthesis prompt used to carry
NOTHING but the placeholder "review failed (no analysis captured)" — and
host-side claude, which inherits devclaw's own cwd, filled the gap with the
WRONG repo: the invented brief persisted to discovery.md and misdirected the
whole goal. These tests pin the fix: a mechanically-collected REPOSITORY
CONTEXT snapshot from ``goal.workspace_dir`` now rides into the prompt
(best-effort, never fatal), together with the anti-inference honesty rules.
"""

from __future__ import annotations

import subprocess

import pytest

from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from tests.goal_fakes import (
    Clock,
    FakeClaude,
    FakeEngine,
    RecordingNotifier,
    fake_prepare,
    seed_goal,
)

BRIEF = (
    "## Current state\nThe review failed — no analysis was captured.\n"
    "## Gap to good\nunknown\n"
    "## What good looks like\n- a working analysis"
)


def _init_bench_repo(repo):
    """A real on-disk .NET+Angular-shaped git checkout — the goal's workspace,
    same fixture shape as #227's end-to-end review-gate guard."""

    def _git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    repo.mkdir()
    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    _git("remote", "add", "origin", "https://github.com/dsdevq/closeloop-bench-fixture.git")
    (repo / "global.json").write_text('{"sdk":{"version":"9.0.315"}}\n')
    (repo / "backend").mkdir()
    (repo / "backend" / "Program.cs").write_text("// entry\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "angular.json").write_text("{}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


def _discovery_in_flight():
    return GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight(
            "devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True,
        ),
    )


async def _tick(store, goal_id, planner, researcher, engine, notifier):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=researcher, notifier=notifier,
        notify_url="", prepare_ws=fake_prepare, eval_every=99,
    )


@pytest.mark.asyncio
async def test_discovery_synthesis_prompt_carries_workspace_snapshot(tmp_path):
    """A FAILED (empty-detail) discovery review on a populated on-disk repo:
    the synthesis prompt must still carry grounded workspace facts (key-file
    presence, the real remote, tracked layout) plus the honesty rules, so the
    model says "the analysis failed" instead of inventing another codebase."""
    store = _store(tmp_path)
    repo = tmp_path / "bench-checkout"
    _init_bench_repo(repo)
    seed_goal(tmp_path, "g", workspace_dir=str(repo))
    store.save_status("g", _discovery_in_flight())
    planner = FakeClaude()  # must not run this tick
    researcher = FakeClaude(BRIEF)
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="failed", detail=""))
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, researcher, engine, notifier)

    assert out is Outcome.ADVANCED
    assert researcher.calls == 1 and planner.calls == 0
    prompt = researcher.last_prompt
    # The failure placeholder is still surfaced honestly as the analysis...
    assert "review failed (no analysis captured)" in prompt
    # ...but the prompt now ALSO carries mechanical facts from the real checkout:
    assert "REPOSITORY CONTEXT (facts collected mechanically" in prompt
    assert "global.json: file" in prompt
    assert "https://github.com/dsdevq/closeloop-bench-fixture.git" in prompt
    assert "backend" in prompt and "frontend" in prompt  # tracked_top_level
    # ...and the anti-inference honesty rules:
    assert "missing or failed" in prompt
    assert "working directory" in prompt


@pytest.mark.asyncio
async def test_discovery_snapshot_is_best_effort_never_fatal(tmp_path):
    """Snapshot collection degrading (workspace dir doesn't exist) must not
    fail the settle — synthesis still runs with the real analysis, the brief is
    written, and the goal advances. Nothing raises."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", workspace_dir=str(tmp_path / "nope" / "missing"))
    store.save_status("g", _discovery_in_flight())
    planner = FakeClaude()
    researcher = FakeClaude(BRIEF)
    engine = FakeEngine(
        poll_result=PollResult(terminal=True, status="done", detail="repo has 3 endpoints"),
    )
    notifier = RecordingNotifier()

    out = await _tick(store, "g", planner, researcher, engine, notifier)

    assert out is Outcome.ADVANCED
    assert researcher.calls == 1
    assert "3 endpoints" in researcher.last_prompt  # the real analysis still fed in
    assert "Current state" in store.read_discovery("g")  # brief persisted
    assert store.load_status("g").lifecycle == "executing"
