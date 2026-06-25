"""Measured pass-rate driver — single-task implement_feature/fix_bug on a REAL repo.

The June-15 "measured pass-rate" must-have: run a basket of real, machine-
verifiable backend tasks on lifekit-dashboard through the REAL engine
(run_sandcastle → docker sandbox → OpenHands → claude), gate each with
`cd backend && dotnet test`, deliver each as a PR (open_pr) — and report the
rate. This also live-validates the PR push, the one delivery part still only
unit-tested.

Wiring mirrors the server exactly: StateStore + TaskQueue(runner=run_sandcastle).
Each task runs on its OWN fresh clone of the repo so the delivered branches/PRs
don't collide. Sequential (concurrency 1) for quota safety + clean rate-limiting.

Run (env MUST be set before import — the runner reads the image/model at import):
    DEVCLAW_SANDBOX_IMAGE=devclaw-sandbox-dotnet \
    DEVCLAW_EXEC_MODEL=claude-sonnet-4-6 \
    .venv/bin/python evals/measure_passrate.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# Import AFTER the caller has set DEVCLAW_SANDBOX_IMAGE / DEVCLAW_EXEC_MODEL —
# sandcastle_runner reads them at module import time.
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from devclaw.engine.sandcastle import run_sandcastle, SANDBOX_IMAGE, EXEC_MODEL

REPO_URL = os.environ.get("MEASURE_REPO_URL", "https://github.com/dsdevq/lifekit-dashboard.git")
VERIFY_CMD = os.environ.get("MEASURE_VERIFY_CMD", "cd backend && dotnet test")
WORKROOT = Path(os.environ.get("MEASURE_WORKROOT", str(Path.home() / "projects" / ".devclaw-measure")))
REPORT_DIR = Path(__file__).resolve().parent / "runs"

# If set, each task workspace is pinned to this SHA before dispatch — so a re-run
# measures the model against the same starting state as the original 5/5
# (2026-06-01). Without pinning, tasks whose tickets have since been merged to
# main return measurement-broken numbers because the impl + tests already exist.
# See ~/memory/projects/devclaw/plan.md → "Measurement direction — L8 telemetry
# is the future, L4 has a sunset". For the 5/5 baseline use
# c09bee5551cd9370b5807b9c3b228dd19fcadf22 (lifekit-dashboard main just before
# PR #10 was merged).
PIN_SHA = os.environ.get("MEASURE_PIN_SHA")

# The basket — real lifekit-dashboard backend tickets, gate-verifiable via
# `dotnet test`, comparable in size to the proven GET /api/decisions feature.
# Every ticket tells the engineer to ADD tests for its change and NOT weaken the
# existing suite, so the gate is a meaningful pass/fail (not trivially gamed).
_TEST_DISCIPLINE = (
    " Add focused xUnit test(s) for this change to the LifekitDashboard.Tests "
    "project so the behaviour is covered by `dotnet test`. Do NOT delete, skip, "
    "or weaken any existing test. The full backend test suite must pass."
)

BASKET = [
    {
        "id": "crons-by-id",
        "kind": "implement_feature",
        "goal": (
            "Add a `GET /api/crons/{id}` endpoint that returns the single cron "
            "with that id (the same shape an element of `GET /api/crons` has), "
            "and returns 404 Not Found when no cron has that id." + _TEST_DISCIPLINE
        ),
    },
    {
        "id": "proposals-status-filter",
        "kind": "implement_feature",
        "goal": (
            "Add an optional `status` query parameter to `GET /api/proposals` so "
            "`GET /api/proposals?status=<x>` returns only proposals whose status "
            "matches (case-insensitive); with no query parameter the endpoint "
            "behaves exactly as before (returns all)." + _TEST_DISCIPLINE
        ),
    },
    {
        "id": "gaps-summary",
        "kind": "implement_feature",
        "goal": (
            "Add a `GET /api/gaps/summary` endpoint that returns a JSON object "
            "with the total number of gaps (e.g. `{ \"total\": <n> }`), derived "
            "from the same data `GET /api/gaps` returns." + _TEST_DISCIPLINE
        ),
    },
    {
        "id": "health-ready",
        "kind": "implement_feature",
        "goal": (
            "Add a `GET /api/health/ready` readiness endpoint, separate from the "
            "existing `/api/health`. It checks that the configured life-root "
            "directory exists and is accessible; return 200 with "
            "`{ \"status\": \"ready\", ... }` when it is, and 503 with "
            "`{ \"status\": \"not-ready\", ... }` when it is not." + _TEST_DISCIPLINE
        ),
    },
    {
        "id": "reject-unknown-404",
        "kind": "fix_bug",
        "goal": (
            "Harden `POST /api/proposals/{slug}/reject`: when the slug does not "
            "match any existing proposal it should return 404 Not Found (a clear "
            "not-found result), not a 500 or a misleading success. First read the "
            "current behaviour, then make the smallest change that guarantees the "
            "unknown-slug case returns 404." + _TEST_DISCIPLINE
        ),
    },
]


async def _run_one(queue: TaskQueue, store: StateStore, task: dict) -> dict:
    ws = WORKROOT / task["id"]
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    pin_note = f" (pinned to {PIN_SHA[:8]})" if PIN_SHA else ""
    print(f"\n=== [{task['id']}] cloning {REPO_URL} → {ws}{pin_note}", flush=True)
    # Drop --depth 1 when pinning so we can checkout the historical SHA.
    clone_cmd = ["git", "clone", REPO_URL, str(ws)] if PIN_SHA else \
                ["git", "clone", "--depth", "1", REPO_URL, str(ws)]
    clone = subprocess.run(clone_cmd, capture_output=True, text=True)
    if clone.returncode != 0:
        return {"id": task["id"], "status": "clone-failed", "error": clone.stderr[-500:]}

    if PIN_SHA:
        co = subprocess.run(
            ["git", "-C", str(ws), "checkout", "-q", PIN_SHA],
            capture_output=True, text=True,
        )
        if co.returncode != 0:
            return {"id": task["id"], "status": "pin-failed", "error": co.stderr[-500:]}

    t0 = time.time()
    print(f"=== [{task['id']}] submitting {task['kind']} (gate=`{VERIFY_CMD}`, open_pr)", flush=True)
    tid = queue.submit(
        kind=task["kind"], workspace_dir=str(ws), goal=task["goal"],
        verify_cmd=VERIFY_CMD, deliver=True,
    )
    await queue.drain()
    wall = round(time.time() - t0, 1)

    row = store.get_task(tid)
    result = json.loads(row.result_json) if row and row.result_json else {}
    verify = result.get("verify") or {}
    rec = {
        "id": task["id"],
        "kind": task["kind"],
        "task_id": tid,
        "status": row.status if row else "?",
        "verify_passed": verify.get("passed"),
        "verify_exit": verify.get("exit_code"),
        "pr_url": row.pr_url if row else None,
        "error": (row.error or "")[:600] if row and row.error else None,
        "wall_s": wall,
        "workspace": str(ws),
    }
    print(f"=== [{task['id']}] → status={rec['status']} verify={rec['verify_passed']} "
          f"pr={rec['pr_url']} wall={wall}s", flush=True)
    return rec


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Comma-separated subset of basket IDs to run (default: all)")
    args = parser.parse_args()
    basket = BASKET
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        basket = [t for t in BASKET if t["id"] in wanted]
        unknown = wanted - {t["id"] for t in BASKET}
        if unknown:
            raise SystemExit(f"unknown basket IDs: {sorted(unknown)}")
        if not basket:
            raise SystemExit("--only matched no basket tasks")

    print(f"image={SANDBOX_IMAGE} exec_model={EXEC_MODEL} repo={REPO_URL}", flush=True)
    print(f"running {len(basket)} task(s): {[t['id'] for t in basket]}", flush=True)
    store = StateStore(str(WORKROOT / "measure.db"))
    queue = TaskQueue(store, runner=run_sandcastle)

    records = []
    for task in basket:
        records.append(await _run_one(queue, store, task))

    done = [r for r in records if r["status"] == "done"]
    rate = round(len(done) / len(records), 3) if records else 0.0
    summary = {
        "image": SANDBOX_IMAGE,
        "exec_model": EXEC_MODEL,
        "repo": REPO_URL,
        "verify_cmd": VERIFY_CMD,
        "n": len(records),
        "pass_rate": rate,
        "done": len(done),
        "prs": [r["pr_url"] for r in records if r.get("pr_url")],
        "records": records,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = REPORT_DIR / f"passrate-{stamp}.json"
    report.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 60)
    print(f"PASS-RATE: {len(done)}/{len(records)} = {rate}")
    for r in records:
        print(f"  {r['id']:<24} {r['status']:<10} verify={r['verify_passed']} pr={r.get('pr_url')}")
    print(f"PRs opened: {summary['prs']}")
    print(f"report: {report}")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
