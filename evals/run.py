#!/usr/bin/env python3
"""Build-from-scratch eval harness — run the REAL pipeline N times and score it.

Drives a live DevClaw HTTP server (real claude + docker) through the full
build_project → grill → approve → build loop, with the grill answered from a
fixed script so the spec is held (roughly) constant and we measure the *build*.
Each run is graded against the project's acceptance check and archived; the
aggregate pass-rate is the success metric.

This is NOT unit-tested (it needs the live engine) — the scoring it relies on is,
in `devclaw/evals.py`. Run it from a checkout with the server already up.

Usage:
    # server running:  DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp
    python evals/run.py evals/json-yaml-cli --n 3 --url http://127.0.0.1:8000/mcp

A project dir holds:
    idea.txt      the build_project idea (verbatim)
    answers.txt   one scripted grill answer per line (extras → "use your recommendation")
    accept.sh     run in the built workspace; exit 0 = pass
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastmcp import Client

# devclaw is pip-installed (pip install -e .); import the scoring core.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from devclaw.evals import Scorecard, aggregate, next_answer, score  # noqa: E402
from devclaw.eval_judge import judge_run, summarize_verdicts  # noqa: E402

POLL_SECONDS = 5.0


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "nogit"


async def _call(client: Client, tool: str, **args) -> dict:
    res = await client.call_tool(tool, args)
    return json.loads(res.content[0].text)


async def _run_once(
    *,
    run_idx: int,
    url: str,
    idea: str,
    answers: list[str],
    accept: Path,
    timeout_s: float,
    stuck_s: float,
    judge: bool = False,
) -> tuple[Scorecard, dict]:
    workspace = tempfile.mkdtemp(prefix=f"eval-{run_idx}-")
    t0 = time.monotonic()
    program_id: str | None = None
    project: dict = {}

    async with Client(url) as client:
        # 1. grill loop (scripted)
        r = await _call(client, "build_project", idea=idea, workspace_dir=workspace)
        project_id = r["project_id"]
        turn = 0
        while r.get("status") == "eliciting":
            if turn > len(answers) + 30:  # safety: the cap should finalize well before this
                break
            r = await _call(client, "answer_question", project_id=project_id, answer=next_answer(answers, turn))
            turn += 1

        if r.get("status") != "ready":
            wall = int((time.monotonic() - t0) * 1000)
            card = score(run=run_idx, program={"status": "no-spec"}, tasks=[], acceptance_passed=None, wall_ms=wall, stuck=True)
            return card, {"project_id": project_id, "phase": "grill-failed", "last": r}

        # 2. approve → build
        a = await _call(client, "approve_spec", project_id=project_id)
        program_id = a["program_id"]

        # 3. poll to terminal (or timeout / stuck)
        last_settled = -1
        last_progress = time.monotonic()
        program, tasks = {}, []
        while True:
            g = await _call(client, "get_program", program_id=program_id)
            program, tasks = g["program"], g["tasks"]
            settled = sum(1 for t in tasks if t["status"] in ("done", "failed"))
            if settled != last_settled:
                last_settled, last_progress = settled, time.monotonic()
            if program["status"] in ("done", "failed"):
                break
            now = time.monotonic()
            if now - t0 > timeout_s:
                program["status"] = "timeout"
                break
            if now - last_progress > stuck_s:
                program["status"] = "timeout"
                break
            await asyncio.sleep(POLL_SECONDS)

        project = await _call(client, "get_project", project_id=project_id)
        events: list[dict] = []
        if judge and program_id:
            try:
                ev = await client.call_tool("get_events", {"program_id": program_id, "limit": 2000})
                events = json.loads(ev.content[0].text)
            except Exception:
                events = []

    # 4. acceptance check (only if the build completed)
    acceptance: bool | None = None
    accept_output = ""
    if program.get("status") == "done":
        proc = subprocess.run(["bash", str(accept)], cwd=workspace, capture_output=True, text=True)
        acceptance = proc.returncode == 0
        accept_output = (proc.stdout or "") + (proc.stderr or "")

    wall = int((time.monotonic() - t0) * 1000)
    stuck = program.get("status") == "timeout"
    card = score(run=run_idx, program=program, tasks=tasks, acceptance_passed=acceptance, wall_ms=wall, stuck=stuck)

    # 5. optional failure-analysis judge (a separate claude call)
    verdict: dict | None = None
    if judge:
        try:
            verdict = await judge_run(
                spec=project.get("spec"), program=program, tasks=tasks, events=events,
                acceptance=acceptance, accept_output=accept_output,
            )
        except Exception as err:
            verdict = {"category": "other", "error": str(err)}
    return card, {
        "project_id": project.get("id"),
        "workspace": workspace,
        "spec": project.get("spec"),
        "verdict": verdict,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", help="e.g. evals/json-yaml-cli")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--timeout", type=float, default=1800, help="per-run wall cap (s)")
    ap.add_argument("--stuck", type=float, default=300, help="no-progress timeout (s)")
    ap.add_argument("--out", default="evals/runs")
    ap.add_argument("--judge", action="store_true", help="run the failure-analysis judge per run (extra claude call)")
    args = ap.parse_args()

    proj = Path(args.project_dir)
    idea = (proj / "idea.txt").read_text().strip()
    answers = [ln for ln in (proj / "answers.txt").read_text().splitlines() if ln.strip()]
    accept = proj / "accept.sh"
    sha = _git_sha()
    out = Path(args.out) / sha / proj.name
    out.mkdir(parents=True, exist_ok=True)

    cards: list[Scorecard] = []
    verdicts: list[dict] = []
    for i in range(1, args.n + 1):
        print(f"[run {i}/{args.n}] …", flush=True)
        try:
            card, detail = await _run_once(
                run_idx=i, url=args.url, idea=idea, answers=answers, accept=accept,
                timeout_s=args.timeout, stuck_s=args.stuck, judge=args.judge,
            )
        except Exception as err:
            print(f"  run {i} errored: {err}", flush=True)
            card = score(run=i, program={"status": "error"}, tasks=[], acceptance_passed=None, wall_ms=0, stuck=False)
            detail = {"error": str(err)}
        cards.append(card)
        if detail.get("verdict"):
            verdicts.append(detail["verdict"])
        (out / f"run-{i}.json").write_text(json.dumps({"score": card.to_dict(), "detail": detail}, indent=2))
        v = detail.get("verdict") or {}
        print(
            f"  status={card.program_status} accept={card.acceptance_passed} "
            f"milestones={card.milestone_done}/{card.milestone_total} "
            f"tasks={card.tasks_done}/{card.tasks_total} wall={card.wall_ms//1000}s"
            + (f" judge={v.get('category')}" if v else ""),
            flush=True,
        )

    summary = aggregate(cards)
    summary["git_sha"] = sha
    summary["project"] = proj.name
    if verdicts:
        summary["failure_analysis"] = summarize_verdicts(verdicts)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nartifacts: {out}")


if __name__ == "__main__":
    asyncio.run(main())
