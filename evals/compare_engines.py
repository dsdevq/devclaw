#!/usr/bin/env python3
"""Engine spike — run the same task suite through OpenHands and the Claude-SDK
engine, side by side, and print a comparison.

The two engines:
  - ``run_sandcastle``  — production: OpenHands SDK inside the sandcastle docker
  - ``run_claude_sdk``  — spike: ``claude --print`` inside the same sandcastle

Both run with the same auth posture (curated ~/.claude allowlist), the same
sandbox image, the same workspace bind. The only difference is the in-container
process. If the SDK path matches OpenHands on pass rate at substantially less
maintenance surface (the openhands-runner script + pinned SDK), drop OpenHands.

Run on a host with:
  - logged-in `claude` CLI (Pro/Max OAuth)
  - docker + the devclaw-sandbox image
  - read/write access to the test repos used in the suite

Usage:
  .venv/bin/python evals/compare_engines.py \\
      --workspace /tmp/spike-ws \\
      --task "Add a /health endpoint to backend that returns {ok: true}" \\
      --verify "cd backend && dotnet test"

Or feed the same task basket measure_passrate.py uses by piping its task list in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from devclaw.engine.claude_sdk import run_claude_sdk
from devclaw.engine import EngineRequest
from devclaw.engine.sandcastle import run_sandcastle


@dataclass
class RunResult:
    engine: str
    task: str
    seconds: float
    status: str
    gate_passed: bool | None
    agent_output_len: int
    error: str | None


async def run_one(engine_name: str, runner, task: str, workspace: str, verify: str | None) -> RunResult:
    started = time.monotonic()
    events: list = []
    req = EngineRequest(
        kind="implement_feature",
        workspace_dir=workspace,
        goal=task,
        on_event=events.append,
        verify_cmd=verify,
    )
    try:
        result = await runner(req)
    except Exception as exc:  # noqa: BLE001
        return RunResult(
            engine=engine_name, task=task,
            seconds=time.monotonic() - started,
            status="exception", gate_passed=None,
            agent_output_len=0, error=f"{type(exc).__name__}: {exc}",
        )

    verify_block = result.get("verify") if isinstance(result, dict) else None
    return RunResult(
        engine=engine_name, task=task,
        seconds=time.monotonic() - started,
        status=str(result.get("status", "?")),
        gate_passed=bool(verify_block.get("passed")) if verify_block else None,
        agent_output_len=len(str(result.get("agent_output", ""))),
        error=result.get("error") if isinstance(result, dict) else None,
    )


async def main_async(args: argparse.Namespace) -> int:
    suite = [(args.task, args.verify)] if args.task else _load_suite(args.suite_file)
    if not suite:
        print("no tasks; pass --task or --suite-file", file=sys.stderr)
        return 2

    out: list[RunResult] = []
    for i, (task, verify) in enumerate(suite, 1):
        # Each engine runs in its own pristine workspace dir so they don't
        # contaminate each other.
        ws_oh = f"{args.workspace}/oh-{i:02d}"
        ws_sdk = f"{args.workspace}/sdk-{i:02d}"
        Path(ws_oh).mkdir(parents=True, exist_ok=True)
        Path(ws_sdk).mkdir(parents=True, exist_ok=True)

        if args.repo:
            os.system(f"git clone --depth=1 {args.repo} {ws_oh} >/dev/null 2>&1")
            os.system(f"git clone --depth=1 {args.repo} {ws_sdk} >/dev/null 2>&1")

        print(f"[{i}/{len(suite)}] OpenHands: {task[:80]}", file=sys.stderr)
        out.append(await run_one("openhands", run_sandcastle, task, ws_oh, verify))
        print(f"[{i}/{len(suite)}] Claude-SDK: {task[:80]}", file=sys.stderr)
        out.append(await run_one("claude_sdk", run_claude_sdk, task, ws_sdk, verify))

    # write the raw report
    Path("evals/runs").mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = Path(f"evals/runs/compare-engines-{stamp}.json")
    report.write_text(json.dumps([asdict(r) for r in out], indent=2))

    # one-line comparison per engine
    for engine in ("openhands", "claude_sdk"):
        runs = [r for r in out if r.engine == engine]
        gates = [r.gate_passed for r in runs if r.gate_passed is not None]
        passrate = f"{sum(gates)}/{len(gates)}" if gates else "n/a"
        mean_s = sum(r.seconds for r in runs) / len(runs) if runs else 0
        print(
            f"{engine:>10s}: passrate={passrate}  mean={mean_s:6.1f}s  "
            f"errors={sum(1 for r in runs if r.error)}/{len(runs)}",
            file=sys.stderr,
        )
    print(f"raw: {report}", file=sys.stderr)
    return 0


def _load_suite(path: str | None) -> list[tuple[str, str | None]]:
    if not path:
        return []
    items: list[tuple[str, str | None]] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # task<TAB>verify_cmd; verify optional
        parts = line.split("\t", 1)
        items.append((parts[0], parts[1] if len(parts) == 2 else None))
    return items


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, help="Parent dir for per-run workspaces (created fresh per task)")
    p.add_argument("--repo", help="If set, git-clone this repo into each per-task workspace before the engines run")
    p.add_argument("--task", help="One task brief. Use --suite-file for a batch.")
    p.add_argument("--verify", help="Verify command to run after the agent finishes")
    p.add_argument("--suite-file", help="Tab-separated task list: <brief>\\t<verify_cmd>, one per line, # comments allowed")
    sys.exit(asyncio.run(main_async(p.parse_args())))
