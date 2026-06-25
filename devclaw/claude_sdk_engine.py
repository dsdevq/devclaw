"""Claude-SDK engine — spike alternative to OpenHands.

A direct ``claude --print`` agent inside the same sandcastle container, no
OpenHands SDK in the picture. The point of the spike is to measure whether a
substantially smaller engine (~150 lines vs OpenHands SDK + the
``openhands-runner/`` script + a pinned 1.24.0 dependency) hits the same
5/5 build-from-scratch pass rate the production sandcastle engine does.

Same sandbox posture as :mod:`devclaw.sandcastle_runner`:
  - per-task ephemeral ``docker run --rm`` against ``devclaw-sandbox:latest``
  - workspace bind-mounted at ``/workspace``
  - curated allowlist under ``~/.claude`` read-only (auth in, nothing else)
  - API-key envs stripped (Pro OAuth posture)
  - verify gate runs INSIDE the container after the agent finishes

What's different:
  - one process inside the container — ``claude --print`` — not the
    OpenHands runner
  - no event stream protocol; we stream the claude CLI's stdout line-by-line
    as ``StdoutLine`` events (so the dashboard still ticks)
  - no agent-loop iteration cap, no stuck detection — claude's own session
    governance handles those

Enable per-run via ``DEVCLAW_ENGINE=claude_sdk`` (wired in ``server/_state.py``).
Decide whether to make it the default after the eval comparison.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
import uuid
from pathlib import Path

from .engine import EngineEvent, EngineRequest, EngineResult
from .runner_io import STREAM_LINE_LIMIT
from .sandcastle_runner import (
    CONTAINER_CLAUDE_DIR,
    CONTAINER_WORKSPACE,
    DOCKER_BIN,
    SANDBOX_CLAUDE_ALLOWLIST,
    SANDBOX_CPUS,
    SANDBOX_IMAGE,
    SANDBOX_MEMORY,
    _build_claude_mounts,
    _strip_api_keys,
    _teardown,
    _translate_workspace_path,
)

# Heavy-coding model id (same lever as the OpenHands path) — Claude Code's
# --model expects an alias OR full id; empty → account default.
EXEC_MODEL = os.environ.get("DEVCLAW_EXEC_MODEL", "claude-sonnet-4-6") or None

# Per-task wall-clock for the agent run (excluding verify). Mirrors the
# OpenHands path's bound; the orchestration owns timeout policy.
AGENT_TIMEOUT_S = int(os.environ.get("DEVCLAW_AGENT_TIMEOUT_S", "1800"))


_PROMPT_SLUGS = {
    "implement_feature": "sdk-implement-feature",
    "fix_bug": "sdk-fix-bug",
    "review_repository": "sdk-review-repository",
    "onboard": "sdk-onboard",
}


def _prompt(req: EngineRequest) -> str:
    from .prompts import load_prompt

    slug = _PROMPT_SLUGS.get(req.kind, "sdk-implement-feature")
    return load_prompt(slug, workspace=CONTAINER_WORKSPACE, goal=req.goal)


def _build_docker_args(
    *,
    container_name: str,
    host_bind_path: str,
    claude_dir: str,
    prompt: str,
    verify_cmd: str | None,
) -> list[str]:
    """``docker run`` argv for one claude-sdk task. The container's command is a
    shell pipeline that runs ``claude --print``, then (if requested) the verify
    gate, then emits a single ``result:`` line on stdout — mirroring the protocol
    the OpenHands runner uses, so :mod:`runner_io` parses both engines."""
    claude_model_flag = f"--model {shlex.quote(EXEC_MODEL)}" if EXEC_MODEL else ""
    # The agent's stdout is the event stream (one line per chunk). The verify
    # gate's stdout/stderr go into a result-line payload. We use sentinels in
    # the container shell so the host parser can separate streams cleanly.
    inner = (
        f"set -o pipefail\n"
        f"cd {shlex.quote(CONTAINER_WORKSPACE)} || exit 91\n"
        # the agent run — claude reads its prompt from stdin so we don't expose it
        # in argv (which would show up in ps + container labels)
        f"echo {shlex.quote(prompt)} | claude --print {claude_model_flag} 2>&1\n"
        f"agent_ec=$?\n"
    )
    if verify_cmd:
        inner += (
            f"echo '__VERIFY_BEGIN__'\n"
            f"( {verify_cmd} ) 2>&1\n"
            f"verify_ec=$?\n"
            f"echo \"result: {{\\\"status\\\":\\\"ok\\\",\\\"workspaceDir\\\":\\\"{CONTAINER_WORKSPACE}\\\""
            f",\\\"agent_exit\\\":$agent_ec,\\\"verify\\\":{{\\\"ran\\\":true,\\\"cmd\\\":\\\"$(printf %s {shlex.quote(verify_cmd)} | sed 's/\"/\\\\\\\"/g')\\\",\\\"exit_code\\\":$verify_ec,\\\"passed\\\":$([ $verify_ec -eq 0 ] && echo true || echo false)}}}}\"\n"
        )
    else:
        inner += (
            f"echo \"result: {{\\\"status\\\":\\\"ok\\\",\\\"workspaceDir\\\":\\\"{CONTAINER_WORKSPACE}\\\""
            f",\\\"agent_exit\\\":$agent_ec}}\"\n"
        )

    return [
        "run",
        "--rm",
        "--name", container_name,
        "--network", "host",  # claude OAuth refresh needs egress
        "--memory", SANDBOX_MEMORY, "--memory-swap", SANDBOX_MEMORY,
        "--cpus", SANDBOX_CPUS,
        "-v", f"{host_bind_path}:{CONTAINER_WORKSPACE}",
        *_build_claude_mounts(claude_dir, SANDBOX_CLAUDE_ALLOWLIST),
        "--tmpfs", f"{CONTAINER_CLAUDE_DIR}/session-env:rw,exec",
        "--tmpfs", f"{CONTAINER_CLAUDE_DIR}/shell-snapshots:rw,exec",
        # Refuse API-key drift — same belt + suspenders as the OpenHands path.
        SANDBOX_IMAGE,
        "bash", "-lc", inner,
    ]


async def _stream_output(
    proc: "asyncio.subprocess.Process",
    on_event,
) -> tuple[EngineResult, str]:
    """Read the container's stdout line by line. Lines before ``__VERIFY_BEGIN__``
    are agent output (emitted as StdoutLine events); after the marker we collect
    verify output for the result. The final ``result: {...}`` line is parsed as
    the EngineResult."""
    agent_lines: list[str] = []
    verify_lines: list[str] = []
    result: EngineResult | None = None
    phase = "agent"
    assert proc.stdout is not None

    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line.startswith("result: "):
            try:
                result = json.loads(line[len("result: "):])
            except json.JSONDecodeError as err:
                result = {"status": "error", "error": f"unparseable result: {err}: {line!r}"}
            continue
        if line == "__VERIFY_BEGIN__":
            phase = "verify"
            continue
        if phase == "agent":
            agent_lines.append(line)
            if on_event is not None:
                on_event(EngineEvent(
                    id=None, type="StdoutLine", source="claude-sdk",
                    ts=int(time.time() * 1000), payload={"line": line},
                ))
        else:
            verify_lines.append(line)

    # Surface the captured stdout on the result so the goal layer / deliveries
    # can read what the agent actually produced.
    agent_output = "\n".join(agent_lines)
    verify_output = "\n".join(verify_lines)
    if result is None:
        # Container exited without emitting a result line — abnormal.
        result = {
            "status": "error",
            "error": "claude-sdk: no result line emitted",
            "agent_output": agent_output,
        }
    else:
        result.setdefault("agent_output", agent_output)
        if "verify" in result and isinstance(result["verify"], dict):
            result["verify"]["output"] = verify_output

    return result, agent_output


async def run_claude_sdk(req: EngineRequest) -> EngineResult:
    """Run one task inside a fresh sandbox using ``claude --print`` directly.
    Conforms to :class:`devclaw.engine.Engine`."""
    claude_dir = os.environ.get("DEVCLAW_HOST_CLAUDE_DIR") or str(Path.home() / ".claude")
    host_bind_path = _translate_workspace_path(req.workspace_dir)
    container_name = f"devclaw-{uuid.uuid4().hex[:8]}"

    args = _build_docker_args(
        container_name=container_name,
        host_bind_path=host_bind_path,
        claude_dir=claude_dir,
        prompt=_prompt(req),
        verify_cmd=req.verify_cmd,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
            env=_strip_api_keys(dict(os.environ)),
        )
    except OSError as exc:
        return {
            "status": "error",
            "error": (
                f"failed to spawn {DOCKER_BIN}: {exc}. "
                "Is docker installed and the socket reachable from this process?"
            ),
        }

    try:
        result, _agent_output = await asyncio.wait_for(
            _stream_output(proc, req.on_event),
            timeout=AGENT_TIMEOUT_S + 60,  # outer wall: agent budget + verify slop
        )
        return result
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "error": f"claude-sdk: hit the {AGENT_TIMEOUT_S}s+ wall-clock timeout",
        }
    finally:
        if proc.returncode is None:
            await _teardown(proc, container_name)
