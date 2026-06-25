"""Host engine — run OpenHands directly on the host, with NO docker sandbox.

``DEVCLAW_ENGINE=host`` wires this in place of the sandcastle (docker) engine.
It runs ``openhands-runner/runner.py`` as a host subprocess in the task's
workspace, using the host's ``claude`` + ``~/.claude`` session.

⚠ NO ISOLATION. The agent runs as your user with full filesystem access — the
whole point of the sandcastle engine is to prevent exactly that. Use the host
engine only where docker isn't available and the risk is acceptable: local dev,
CI, and live validation of small throwaway builds. Production uses the docker
sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from . import EngineRequest, EngineResult
from .runner_io import STREAM_LINE_LIMIT, consume_runner_output

_REPO = Path(__file__).resolve().parents[1]
# the in-sandbox runner, run here on the host
RUNNER_PY = os.environ.get("DEVCLAW_RUNNER_PY", str(_REPO / "openhands-runner" / "runner.py"))
# the python that has openhands-sdk — the openhands-runner venv if present
_OH_VENV = _REPO / "openhands-runner" / ".venv" / "bin" / "python"
RUNNER_PYTHON = os.environ.get("DEVCLAW_RUNNER_PYTHON") or (
    str(_OH_VENV) if _OH_VENV.exists() else sys.executable
)


def _strip_api_keys(env: dict[str, str]) -> dict[str, str]:
    clean = dict(env)
    clean.pop("ANTHROPIC_API_KEY", None)
    clean.pop("ANTHROPIC_AUTH_TOKEN", None)
    return clean


async def run_host(req: EngineRequest) -> EngineResult:
    """Run one task by invoking the OpenHands runner on the host (no container).
    The workspace path is used as-is (no bind-mount / no path translation)."""
    Path(req.workspace_dir).mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "kind": req.kind,
            "workspace_dir": req.workspace_dir,
            "goal": req.goal,
            # verify gate runs on the host after the agent finishes (host toolchain).
            "verify_cmd": req.verify_cmd,
        }
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            RUNNER_PYTHON,
            RUNNER_PY,
            payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,  # event lines can exceed asyncio's 64 KiB default
            env=_strip_api_keys(dict(os.environ)),
        )
    except OSError as exc:
        return {
            "status": "error",
            "error": f"failed to spawn host runner ({RUNNER_PYTHON} {RUNNER_PY}): {exc}",
        }
    return await consume_runner_output(proc, req.on_event, label="host-runner")
