"""Stub engine + cognition — deterministic, offline, for HARNESS VALIDATION.

When ``DEVCLAW_ENGINE=stub`` the server wires these in place of OpenHands (the
engine) AND claude (the planner). The whole pipeline then runs with no docker
and no claude, so harness tests can exercise everything *around* the agent — the
MCP tools, scheduling, execution wiring, event recording — and prove the plumbing
is sound. A live run then tests only the one thing this can't fake: real agent
quality.

It is NOT a real builder. For a `jyq` goal the stub engine writes a
genuinely-working package so the green path is provable end-to-end; for any
other goal it writes a placeholder (which fails acceptance — exercising the
failure path).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import EngineEvent, EngineRequest, EngineResult
from ..planner import PlannedTask

# ---- engine ----------------------------------------------------------------

_JYQ_MAIN = '''import json
import sys

import yaml


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: jyq <to-yaml|to-json> <file>\\n")
        return 2
    cmd, path = argv
    try:
        text = open(path).read()
    except OSError as e:
        sys.stderr.write(f"error: {e}\\n")
        return 1
    if cmd == "to-yaml":
        sys.stdout.write(yaml.safe_dump(json.loads(text), sort_keys=True))
    elif cmd == "to-json":
        sys.stdout.write(json.dumps(yaml.safe_load(text)))
    else:
        sys.stderr.write(f"unknown command: {cmd}\\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _write_jyq(ws: Path) -> None:
    pkg = ws / "jyq"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(_JYQ_MAIN)


async def stub_engine(req: EngineRequest) -> EngineResult:
    """Deterministic 'build' — writes files into the workspace and returns ok.
    Emits one event so the event-recording path is exercised too."""
    if req.on_event:
        req.on_event(
            EngineEvent(id="stub-1", type="StubBuildEvent", source="stub", ts=0, payload={"goal": req.goal})
        )
    ws = Path(req.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    if "jyq" in req.goal.lower():
        _write_jyq(ws)
        message = "stub: wrote a working jyq package"
    else:
        (ws / "STUB_BUILD.txt").write_text(f"stub build for goal: {req.goal}\n")
        message = "stub: wrote a placeholder (no recipe for this goal)"
    return {"status": "ok", "workspaceDir": req.workspace_dir, "message": message}


# ---- cognition (planner) ----------------------------------------------------


async def stub_goal_planner(goal: str, workspace_dir: str) -> list[PlannedTask]:
    return [PlannedTask(key="t1", goal=goal, kind="implement_feature", depends_on_keys=[])]
