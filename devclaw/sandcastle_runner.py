"""Per-task docker sandbox runner.

Spawns ``docker run --rm`` against the devclaw-sandbox image for each task. The
container's ENTRYPOINT runs the OpenHands runner (``openhands-runner/runner.py``),
which streams one prefixed JSON line per event (``event: {...}``) plus a single
terminating ``result: {...}`` line. This module:

  - Translates an ``OpenHandsRequest`` into a docker invocation.
  - Bind-mounts the host workspace into /workspace and ~/.claude read-only into
    /home/agent/.claude (Pro OAuth posture: the claude CLI inside the sandbox
    can read tokens but not write back).
  - Streams stdout line-by-line; routes ``event:`` lines through ``on_event``
    and parses the final ``result:`` line as the result.
  - Refuses to forward ANTHROPIC_API_KEY into the container (same belt +
    suspenders the runner enforces).

Container lifecycle: --rm + the per-task --name make destroy-on-exit automatic;
no persistent on-host state. Tests inject a stub runner (via TaskQueue's
``runner`` param) so they don't need docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from .state_store import TaskKind

SANDBOX_IMAGE = os.environ.get("DEVCLAW_SANDBOX_IMAGE", "devclaw-sandbox:latest")
DOCKER_BIN = os.environ.get("DEVCLAW_DOCKER_BIN", "docker")
# Container-side mount targets. Match the Dockerfile's expectations.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CLAUDE_DIR = "/home/agent/.claude"


@dataclass
class OpenHandsRequest:
    """Inputs the runner needs to launch one OpenHands run. The same kinds the
    MCP tool surface exposes; runner.py picks the right system-prompt per kind."""

    kind: TaskKind
    workspace_dir: str
    goal: str
    #: optional callback, one call per ``event:`` line the runner emits
    on_event: Optional[Callable[["RunnerEvent"], None]] = None


@dataclass
class RunnerEvent:
    id: Optional[str]
    type: str
    source: str
    ts: Union[int, str]
    payload: object


# Terminal verdict from one run. Mirrors the ``result: {...}`` line shape that
# runner.py emits. status == "ok" carries workspace_dir/message (+ agent_output
# for debugging); status == "error" carries error (+ optional trace).
OpenHandsResult = dict


class SandcastleRunnerError(Exception):
    def __init__(self, message: str, trace: str | None = None) -> None:
        super().__init__(message)
        self.trace = trace


def _strip_api_keys(env: dict[str, str]) -> dict[str, str]:
    clean = dict(env)
    clean.pop("ANTHROPIC_API_KEY", None)
    clean.pop("ANTHROPIC_AUTH_TOKEN", None)
    return clean


def _translate_workspace_path(workspace_dir: str) -> str:
    """When devclaw itself runs in a container and spawns docker on the host
    socket, the workspace path it sees internally is not the host's view of
    that bind-mounted dir. The path-prefix env pair tells us how to translate.
    Unset -> pass through (typical local dev, running directly on host)."""
    container_prefix = os.environ.get("DEVCLAW_CONTAINER_PATH_PREFIX")
    host_prefix = os.environ.get("DEVCLAW_HOST_PATH_PREFIX")
    if container_prefix and host_prefix and workspace_dir.startswith(container_prefix):
        return host_prefix + workspace_dir[len(container_prefix) :]
    return workspace_dir


async def run_sandcastle(req: OpenHandsRequest) -> OpenHandsResult:
    """Run one task inside a fresh sandbox container. Resolves with an
    OpenHandsResult dict so it's a drop-in runner for TaskQueue."""
    # DEVCLAW_HOST_CLAUDE_DIR is a HOST path passed straight to docker as a bind
    # source. When devclaw-mcp runs in a container, that path intentionally does
    # NOT exist in the container's view — we pass the string through and let
    # docker emit a clear error if the operator misconfigured the env var.
    claude_dir = os.environ.get("DEVCLAW_HOST_CLAUDE_DIR") or str(
        Path.home() / ".claude"
    )
    host_bind_path = _translate_workspace_path(req.workspace_dir)

    # Per-task container name for greppable logs + manual cleanup if --rm fails.
    container_name = f"devclaw-{uuid.uuid4().hex[:8]}"

    payload = json.dumps(
        {"kind": req.kind, "workspace_dir": CONTAINER_WORKSPACE, "goal": req.goal}
    )

    docker_args = [
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "host",  # claude OAuth refresh needs egress; tighten later via allowlist.
        "-v",
        f"{host_bind_path}:{CONTAINER_WORKSPACE}",
        "-v",
        f"{claude_dir}:{CONTAINER_CLAUDE_DIR}:ro",
        "-e",
        "OPENHANDS_SUPPRESS_BANNER=1",
        SANDBOX_IMAGE,
        payload,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN,
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

    result: Optional[OpenHandsResult] = None
    stderr_chunks: list[bytes] = []

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_task = asyncio.ensure_future(drain_stderr())

    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("event: "):
            if req.on_event:
                try:
                    data = json.loads(line[len("event: ") :])
                    req.on_event(
                        RunnerEvent(
                            id=data.get("id"),
                            type=data.get("type", ""),
                            source=data.get("source", ""),
                            ts=data.get("ts", 0),
                            payload=data.get("payload"),
                        )
                    )
                except json.JSONDecodeError as parse_err:
                    # malformed event line — drop, don't crash the run
                    import sys

                    sys.stderr.write(
                        f"sandcastle-runner: dropping malformed event line: {parse_err}\n"
                    )
        elif line.startswith("result: "):
            # first result line wins; ignore anything after
            if result is None:
                try:
                    result = json.loads(line[len("result: ") :])
                except json.JSONDecodeError as parse_err:
                    result = {
                        "status": "error",
                        "error": f"runner emitted unparsable result: {parse_err}",
                        "trace": line,
                    }
        # everything else is decorative sandbox output — drop

    await proc.wait()
    stderr_task.cancel()
    stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace")

    if result is not None:
        return result
    return {
        "status": "error",
        "error": (
            f"sandbox exited {proc.returncode} without a result line. "
            f"stderr tail:\n{stderr_text[-1024:]}"
        ),
    }
