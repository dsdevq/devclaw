#!/usr/bin/env python3
"""Sandbox E2E suite — run every scenario, report pass/fail.

Walks ``evals/sandbox/scenarios/*.yaml`` and runs each through
``sandbox_e2e.py``, collecting per-scenario summaries into one report under
``evals/runs/suite-<timestamp>/``. Exit code is 0 only if every scenario
passes; otherwise 1 with the failing scenarios listed.

Use this before/after a change that touches the runtime path. The full suite
runs against the stub cognition by default — free, deterministic, fast — so
you can put it in CI. Switch to ``--cognition claude`` for a periodic real-run
check that burns quota.

Usage:

  .venv/bin/python evals/run_all.py
  .venv/bin/python evals/run_all.py --cognition claude
  .venv/bin/python evals/run_all.py --only blocked_planner,scope_grill_happy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _add_repo_to_path() -> None:
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


_add_repo_to_path()

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "evals" / "sandbox" / "scenarios"
RUNS_ROOT = REPO_ROOT / "evals" / "runs"


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _all_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))


def _is_stub_only(scenario_id: str) -> bool:
    """A scenario flagged ``stub_only: true`` exercises a path that only the
    fake cognition can reach (e.g. injecting a ``RAISE:`` magic string), so it
    must be skipped under ``--cognition claude``."""
    import yaml

    raw = yaml.safe_load((SCENARIOS_DIR / f"{scenario_id}.yaml").read_text()) or {}
    return bool(raw.get("stub_only"))


async def _run_one(scenario_id: str, cognition: str, suite_dir: Path) -> dict:
    # Import inside to avoid the side effects (DEVCLAW_ENGINE env) bleeding
    # across scenarios; sandbox_e2e is designed for single-run-per-process,
    # but we run it as a subprocess to keep state hygiene strict.
    import subprocess
    out_dir = suite_dir / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(REPO_ROOT / "evals" / "sandbox_e2e.py"),
        "--scenario", scenario_id,
        "--cognition", cognition,
        "--out", str(out_dir),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    summary_path = out_dir / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text())
    else:
        summary = {
            "scenario": scenario_id, "passed": False,
            "errors": [f"runner produced no summary; stderr tail: {stderr.decode(errors='replace')[-300:]}"],
            "expect_failures": [],
        }
    return summary


def _print_report(summaries: list[dict], suite_dir: Path) -> int:
    passed = [s for s in summaries if s["passed"]]
    failed = [s for s in summaries if not s["passed"]]
    print()
    print(f"=== sandbox e2e suite: {len(passed)}/{len(summaries)} passed ===")
    print()
    width = max((len(s["scenario"]) for s in summaries), default=20)
    for s in summaries:
        mark = "✓" if s["passed"] else "✗"
        cog = s.get("cognition_calls", 0)
        disp = s.get("dispatches", 0)
        delv = s.get("deliveries", 0)
        print(f"  {mark} {s['scenario']:<{width}}  cognition={cog}  dispatches={disp}  deliveries={delv}")
        for err in s.get("errors") or []:
            print(f"      ! ERROR: {err}")
        for f in s.get("expect_failures") or []:
            print(f"      ! EXPECT: {f}")
    print()
    print(f"suite artifacts: {suite_dir}")
    return 0 if not failed else 1


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Run the sandbox e2e suite.")
    ap.add_argument("--cognition", choices=("claude", "stub"), default="stub")
    ap.add_argument("--only", default=None,
                    help="comma-separated scenario ids to run (default: all)")
    ap.add_argument("--out", type=Path, default=None,
                    help="suite output dir (default: evals/runs/suite-<timestamp>)")
    args = ap.parse_args()

    scenarios = _all_scenarios()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - set(scenarios)
        if unknown:
            raise SystemExit(f"unknown scenarios: {sorted(unknown)}")
        scenarios = [s for s in scenarios if s in wanted]

    suite_dir = args.out or (RUNS_ROOT / f"suite-{_now_slug()}")
    suite_dir.mkdir(parents=True, exist_ok=True)
    print(f"running {len(scenarios)} scenarios → {suite_dir}")
    print(f"cognition: {args.cognition}")
    print()

    summaries: list[dict] = []
    skipped: list[str] = []
    for sid in scenarios:
        if args.cognition == "claude" and _is_stub_only(sid):
            print(f"  → {sid} ... SKIP (stub_only)", flush=True)
            skipped.append(sid)
            continue
        print(f"  → {sid} ...", flush=True)
        summary = await _run_one(sid, args.cognition, suite_dir)
        summaries.append(summary)
        mark = "✓" if summary["passed"] else "✗"
        print(f"    {mark} {sid}")

    (suite_dir / "report.json").write_text(json.dumps({
        "started_at": suite_dir.name,
        "cognition": args.cognition,
        "scenarios": summaries,
        "skipped_stub_only": skipped,
        "passed": all(s["passed"] for s in summaries),
        "n_passed": sum(1 for s in summaries if s["passed"]),
        "n_failed": sum(1 for s in summaries if not s["passed"]),
    }, indent=2))

    exit_code = _print_report(summaries, suite_dir)
    if skipped:
        print(f"  (skipped under --cognition claude: {', '.join(skipped)})")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
