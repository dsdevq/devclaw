"""Per-task docker sandbox runner — the OpenHands :class:`~devclaw.engine.Engine`.

This is the one concrete Engine implementation (see ``engine.py`` for the seam).
Spawns ``docker run --rm`` against the devclaw-sandbox image for each task. The
container's ENTRYPOINT runs the OpenHands runner (``openhands-runner/runner.py``),
which streams one prefixed JSON line per event (``event: {...}``) plus a single
terminating ``result: {...}`` line. This module:

  - Translates an ``EngineRequest`` into a docker invocation.
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
from pathlib import Path

from .engine import EngineRequest, EngineResult
from .runner_io import consume_runner_output

SANDBOX_IMAGE = os.environ.get("DEVCLAW_SANDBOX_IMAGE", "devclaw-sandbox:latest")
DOCKER_BIN = os.environ.get("DEVCLAW_DOCKER_BIN", "docker")
# The model the in-sandbox OpenHands agent runs on — this is the heavy coding
# path and the bulk of the Pro/Max quota burn, so it defaults to Sonnet (strong
# at code, far lighter than Opus); set DEVCLAW_EXEC_MODEL=claude-opus-4-8 to opt
# a run up to Opus. Passed to the runner, which hands it to ACPAgent as the
# `acp_model` (Claude ACP selects it via session _meta). Must be a full model id,
# not an alias. Empty → the ACP server's default.
EXEC_MODEL = os.environ.get("DEVCLAW_EXEC_MODEL", "claude-sonnet-4-6") or None
# Container-side mount targets. Match the Dockerfile's expectations.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CLAUDE_DIR = "/home/agent/.claude"


class SandcastleRunnerError(Exception):
    def __init__(self, message: str, trace: str | None = None) -> None:
        super().__init__(message)
        self.trace = trace


def _strip_api_keys(env: dict[str, str]) -> dict[str, str]:
    clean = dict(env)
    clean.pop("ANTHROPIC_API_KEY", None)
    clean.pop("ANTHROPIC_AUTH_TOKEN", None)
    return clean


async def _teardown(proc: "asyncio.subprocess.Process", container_name: str) -> None:
    """Best-effort kill of a still-running sandbox — used when the task is
    cancelled (or the stream breaks) before the container exits on its own.
    Killing the ``docker run`` client does NOT stop the container, so we also
    ``docker rm -f`` by name to honour --rm's destroy guarantee. Swallows every
    error, including a re-delivered CancelledError, so cleanup always completes;
    the original cancellation still propagates from the caller's try-block."""
    import sys

    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        killer = await asyncio.create_subprocess_exec(
            DOCKER_BIN,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except asyncio.CancelledError:
        pass
    except Exception as err:  # pragma: no cover - defensive
        sys.stderr.write(
            f"sandcastle-runner: force-remove of {container_name} failed: {err}\n"
        )


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


async def run_sandcastle(req: EngineRequest) -> EngineResult:
    """Run one task inside a fresh sandbox container. An :class:`~devclaw.engine.Engine`
    — resolves with an EngineResult dict so TaskQueue can drive it."""
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
        {
            "kind": req.kind,
            "workspace_dir": CONTAINER_WORKSPACE,
            "goal": req.goal,
            "model": EXEC_MODEL,  # the in-sandbox agent's tier; None → ACP default
        }
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

    try:
        return await consume_runner_output(proc, req.on_event, label="sandbox")
    finally:
        # On cancellation the read above raises CancelledError straight into
        # here with the container still alive — tear it down (docker-specific,
        # so it can't live in the engine-agnostic reader). On a clean exit proc
        # has already returned, so teardown is a cheap no-op.
        if proc.returncode is None:
            await _teardown(proc, container_name)
