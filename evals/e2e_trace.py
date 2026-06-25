#!/usr/bin/env python3
"""E2E trace driver — run a goal end-to-end and dump a structured trace.

The "blind refactor" antidote. Today you can ship a refactor whose unit tests
all pass but whose *runtime* path subtly broke — wrong prompt, missing tick,
extra cognition call. This script runs the full live path against a real goal
and emits two artifacts:

  * ``trace.json``     machine-readable event log (diffable across runs)
  * ``timeline.md``    human-readable per-tick narrative

Usage:

  # Stub mode (no claude, no docker — quick smoke test of the harness wiring):
  .venv/bin/python evals/e2e_trace.py --mode stub --out evals/runs/e2e-stub

  # Live mode (real claude --print, real engine — needs OAuth + the engine env
  # the server uses; dispatches into an existing GoalStore):
  .venv/bin/python evals/e2e_trace.py --mode live \\
      --goals-dir ~/memory/goals \\
      --goal-id <id> \\
      --ticks 5 \\
      --out evals/runs/e2e-live

Live mode does NOT create a goal — it ticks an existing one. Create the goal
via the OpenClaw waiter (which runs scope_grill + create_goal) or by hand with
the MCP server running, then point this at it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional


def _add_path() -> None:
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


_add_path()

from devclaw.loom.trace import Tracer, record_note, set_tracer  # noqa: E402


async def _run_stub(out_dir: Path, ticks: int) -> Tracer:
    """Stub harness: full lifecycle on FakeClaude + FakeEngine. Deterministic,
    no network. Mirrors the structural assertions in ``tests/test_e2e_trace.py``
    and writes the trace artifacts so you can read what the harness produces
    without standing up the server."""
    from devclaw.goal.models import GoalStatus, InFlight, PollResult
    from devclaw.goal.store import GoalStore
    from devclaw.goal.tick import tick_goal
    # tests/ is a sibling package under the repo root added to sys.path above.
    from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

    PLAN_ACT = json.dumps({
        "decision": "act", "note": "ship next",
        "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}],
    })
    PLAN_DONE = json.dumps({"decision": "done", "note": "all backlog shipped"})
    EVAL_ACHIEVED = json.dumps({"verdict": "achieved", "rationale": "all done_when met"})

    tmp = out_dir / "stub-goals"
    tmp.mkdir(parents=True, exist_ok=True)
    store = GoalStore(tmp, now=Clock())
    seed_goal(tmp, "g", backlog=["add /health"])
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="investigating",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "analyze", is_discovery=True),
    ))
    notifier = RecordingNotifier()

    tracer = Tracer(label="stub")
    set_tracer(tracer)
    try:
        # 1) discovery settles → executing
        record_note("tick 1 — discovery settles")
        await tick_goal(
            "g", store=store,
            engine=FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="repo OK")),
            planner_caller=FakeClaude(PLAN_ACT, role="planner"),
            evaluator_caller=FakeClaude("## Current state\nbare API", role="evaluator"),
            notifier=notifier, prepare_ws=fake_prepare,
        )
        # 2) executing → dispatch
        record_note("tick 2 — dispatch implement_feature")
        await tick_goal(
            "g", store=store,
            engine=FakeEngine(
                poll_result=PollResult(terminal=False, status="running"),
                dispatch_ref=InFlight("devclaw", "implement_feature", "task_a", "task", "add /health"),
            ),
            planner_caller=FakeClaude(PLAN_ACT, role="planner"),
            evaluator_caller=FakeClaude(role="evaluator"),
            notifier=notifier, prepare_ws=fake_prepare,
        )
        # 3) action settles → delivery → planner says done → eval verdict achieved
        record_note("tick 3 — action settles green, done-gate evaluates")
        await tick_goal(
            "g", store=store,
            engine=FakeEngine(poll_result=PollResult(
                terminal=True, status="done", detail="merged",
                pr_url="https://example/pr/1", gate_passed=True,
            )),
            planner_caller=FakeClaude(PLAN_DONE, role="planner"),
            evaluator_caller=FakeClaude(EVAL_ACHIEVED, role="evaluator"),
            notifier=notifier, prepare_ws=fake_prepare,
            verify_done=False,  # artifact-only done eval — no extra review dispatch
        )
    finally:
        set_tracer(None)
    return tracer


async def _run_live(out_dir: Path, goals_dir: Path, goal_id: Optional[str], ticks: int) -> Tracer:
    """Live harness: drive the production :class:`GoalService` against a real
    ``DEVCLAW_GOALS_DIR`` for N ticks. No goal creation — point this at an
    existing goal (created via the waiter / MCP server). Real cognition (real
    ``claude --print``) and the configured engine; OAuth + engine env must be
    set as for the server.

    The tracer records every cognition + tick + dispatch + delivery + notify so
    you can diff before/after a refactor."""
    import os
    os.environ.setdefault("DEVCLAW_GOALS_DIR", str(goals_dir))
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    state_db = out_dir / "live-state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(state_db))
    queue = TaskQueue(store)
    cfg = GoalConfig(
        goals_dir=Path(os.path.expanduser(str(goals_dir))),
        notify_url="", tick_seconds=900, eval_every=3, verify_done=True,
    )
    svc = GoalService(queue, store, cfg)

    tracer = Tracer(label=f"live:{goal_id or 'all'}")
    set_tracer(tracer)
    try:
        for i in range(ticks):
            record_note(f"tick {i + 1}/{ticks}")
            if goal_id:
                outcome = await svc.tick_one(goal_id)
                record_note(f"outcome: {outcome}")
                if outcome in ("done", "skip_done", "skip_cancelled"):
                    record_note("terminal — stopping early")
                    break
            else:
                outcomes = await svc.tick_all()
                record_note(f"outcomes: {json.dumps(outcomes)}")
    finally:
        set_tracer(None)
        store.close()
    return tracer


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E goal trace driver.")
    ap.add_argument("--mode", choices=("stub", "live"), default="stub")
    ap.add_argument("--out", type=Path, required=True, help="output directory for trace.json + timeline.md")
    ap.add_argument("--goals-dir", type=Path, default=Path("~/memory/goals").expanduser(),
                    help="(live) DEVCLAW_GOALS_DIR")
    ap.add_argument("--goal-id", type=str, default=None,
                    help="(live) tick this goal only; omit → tick_all")
    ap.add_argument("--ticks", type=int, default=3, help="(live) how many ticks to run")
    args = ap.parse_args()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "stub":
        tracer = asyncio.run(_run_stub(out_dir, args.ticks))
    else:
        tracer = asyncio.run(_run_live(out_dir, args.goals_dir, args.goal_id, args.ticks))

    json_path = tracer.dump_json(out_dir / "trace.json")
    md_path = tracer.dump_timeline(out_dir / "timeline.md")
    summary = {
        "label": tracer.label,
        "events": len(tracer.events),
        "ticks": len(tracer.by_kind("tick")),
        "cognition_calls": len(tracer.by_kind("cognition")),
        "cognition_by_role": tracer.cognition_by_role(),
        "dispatches": len(tracer.by_kind("dispatch")),
        "deliveries": len(tracer.by_kind("delivery")),
        "notifications": len(tracer.by_kind("notify")),
        "json": str(json_path),
        "timeline": str(md_path),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
