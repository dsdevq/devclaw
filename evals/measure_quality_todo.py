"""Quality-characterization driver — harder tasks on the safe todo-fullstack-demo.

Step 1 of the "make green mean trustworthy" dogfood: run a basket of
deliberately HARDER tasks than the small machine-verifiable backend tickets
proven so far — ambiguous spec, multi-file change, a pure-UI/judgment component
whose change the backend gate does NOT cover — through the REAL engine
(run_sandcastle -> docker sandbox -> OpenHands -> claude), gate each with the
backend pytest suite, deliver each as a PR (open_pr; standalone tasks do NOT
auto-merge), then read every diff adversarially.

This is the CURRENT harness (babysitting preamble + _QUALITY_BAR + test-integrity
gate, all deployed) — so the resulting diffs are the honest quality baseline the
new pre-PR review gate will be measured against.

Wiring mirrors measure_passrate.py / the server: StateStore +
TaskQueue(runner=run_sandcastle), each task on its OWN fresh clone, sequential.

Run INSIDE the devclaw-mcp container (has docker + claude OAuth + gh), with the
workroot under the container workspace prefix so sandboxes can mount the clones:
    DEVCLAW_EXEC_MODEL=claude-sonnet-4-6 \
    MEASURE_WORKROOT=/var/lib/devclaw/workspaces/qmeasure \
    python3 /var/lib/devclaw/qdriver/measure_quality_todo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from devclaw.sandcastle_runner import run_sandcastle, SANDBOX_IMAGE, EXEC_MODEL

REPO_URL = os.environ.get("MEASURE_REPO_URL", "https://github.com/dsdevq/todo-fullstack-demo.git")
# Backend pytest is the gate. Install deps first (idempotent) so a fresh sandbox
# can run the suite. The frontend (T3) is deliberately NOT covered by this gate —
# that is the point: a pure-UI change goes "green" without the gate touching it.
VERIFY_CMD = os.environ.get(
    "MEASURE_VERIFY_CMD",
    "cd backend && pip install -q -r requirements.txt && python -m pytest -q",
)
WORKROOT = Path(os.environ.get("MEASURE_WORKROOT", "/var/lib/devclaw/workspaces/qmeasure"))
REPORT_DIR = Path(os.environ.get("MEASURE_REPORT_DIR", str(WORKROOT / "runs")))

# A meaningful-gate suffix for the backend tasks (mirrors the proven basket): add
# real tests, don't weaken the suite. Deliberately NOT a quality lecture — the
# harness already injects _QUALITY_BAR; we want to observe its NATURAL output.
_TEST_DISCIPLINE = (
    " Add focused pytest test(s) for this change to backend/tests so the new "
    "behaviour is covered by `python -m pytest`. Do NOT delete, skip, or weaken "
    "any existing test. The full backend test suite must pass."
)

BASKET = [
    {
        # Multi-file + ambiguous spec: model + schema + endpoints + frontend.
        # Underspecified on purpose (format, timezone, what 'overdue' means,
        # validation of bad/past dates) to see how the agent resolves judgment.
        "id": "due-dates",
        "kind": "implement_feature",
        "goal": (
            "Add support for optional due dates on todos. A todo may have a due "
            "date or none. Users should be able to set a due date when creating a "
            "todo and change it later, see each todo's due date in the list, and "
            "filter the list to show only overdue todos. Implement this across the "
            "backend API (model, schemas, endpoints) and the frontend." + _TEST_DISCIPLINE
        ),
    },
    {
        # Ambiguous judgment, backend-only: bulk ops. Surfaces where logic lands,
        # empty-case handling, return-shape choices, and no-op/dead-code risk.
        "id": "bulk-ops",
        "kind": "implement_feature",
        "goal": (
            "Add bulk operations to the todos API: one endpoint to mark ALL todos "
            "as completed, and one endpoint to delete ALL completed todos. Choose "
            "request/response shapes consistent with the existing API, and handle "
            "the empty case sensibly." + _TEST_DISCIPLINE
        ),
    },
    {
        # Pure UI/judgment: the backend already supports PUT /todos/{id}. The
        # backend gate passes WITHOUT covering any of this change — the sharpest
        # 'green != trustworthy' case, and where a spectator-PO is blindest.
        "id": "inline-edit",
        "kind": "implement_feature",
        "goal": (
            "Improve the frontend (the frontend/ directory). Let users edit a "
            "todo's title inline: double-clicking the title turns it into a text "
            "input, Enter saves the new title via the existing PUT /todos/{id} "
            "endpoint, Escape cancels without saving. Also show each todo's created "
            "date on its row. Keep the existing add / toggle / delete behaviour "
            "working. There is no automated frontend test suite, so verify the "
            "DOM/event logic carefully yourself."
        ),
    },
]


async def _run_one(queue: TaskQueue, store: StateStore, task: dict) -> dict:
    ws = WORKROOT / task["id"]
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n=== [{task['id']}] cloning {REPO_URL} -> {ws}", flush=True)
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", REPO_URL, str(ws)],
        capture_output=True, text=True,
    )
    if clone.returncode != 0:
        return {"id": task["id"], "status": "clone-failed", "error": clone.stderr[-500:]}

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
        "error": (row.error or "")[:800] if row and row.error else None,
        "wall_s": wall,
        "workspace": str(ws),
    }
    print(f"=== [{task['id']}] -> status={rec['status']} verify={rec['verify_passed']} "
          f"pr={rec['pr_url']} wall={wall}s", flush=True)
    return rec


async def main() -> None:
    print(f"image={SANDBOX_IMAGE} exec_model={EXEC_MODEL} repo={REPO_URL}", flush=True)
    WORKROOT.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(WORKROOT / "measure.db"))
    queue = TaskQueue(store, runner=run_sandcastle)

    only = os.environ.get("MEASURE_ONLY")
    basket = [t for t in BASKET if not only or t["id"] in only.split(",")]

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
        "gate_pass_rate": rate,
        "done": len(done),
        "prs": [r["pr_url"] for r in records if r.get("pr_url")],
        "records": records,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = REPORT_DIR / f"quality-{stamp}.json"
    report.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 60)
    print(f"GATE-PASS-RATE: {len(done)}/{len(records)} = {rate}")
    for r in records:
        print(f"  {r['id']:<16} {r['status']:<10} verify={r['verify_passed']} pr={r.get('pr_url')}")
    print(f"PRs opened: {summary['prs']}")
    print(f"report: {report}")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
