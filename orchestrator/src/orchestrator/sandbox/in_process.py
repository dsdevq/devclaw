"""In-process Sandbox adapter — the safety-net fallback.

Wraps today's behaviour: spawn `agent_command` as a direct subprocess of the orchestrator daemon. NO isolation — the agent shares the orchestrator container's filesystem, /tmp, network, and host credentials. Kept because:

  1. It is the only adapter that works when Docker is unavailable (local dev, CI smoke tests, host-side dry runs).
  2. It is the fallback if Sandcastle init fails before the first task ever runs (we'd rather degrade than wedge the daemon).

`readonly_mounts` is intentionally ignored here — there is no container boundary to mount against; the host filesystem is already visible to the child.
"""

from __future__ import annotations

import logging
import os
import subprocess

from orchestrator.sandbox.base import BranchStrategy, SandboxResult

logger = logging.getLogger(__name__)


class InProcessSandbox:
    """Subprocess-only adapter. See module docstring."""

    name = "in_process"

    def run(
        self,
        task_id: str,
        repo_url: str,
        branch_strategy: BranchStrategy,
        agent_command: list[str],
        env: dict[str, str],
        readonly_mounts: dict[str, str],
        timeout_seconds: int,
    ) -> SandboxResult:
        merged_env = {**os.environ, **env}

        try:
            completed = subprocess.run(
                agent_command,
                capture_output=True,
                text=True,
                env=merged_env,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial_stdout = exc.stdout
            partial_stderr = exc.stderr
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode("utf-8", errors="replace")
            return SandboxResult(
                returncode=-1,
                stdout=partial_stdout or "",
                stderr=partial_stderr or "",
                timed_out=True,
                runtime="in_process",
                notes=[f"timeout after {timeout_seconds}s"],
            )
        except FileNotFoundError as exc:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"command not found: {exc}",
                runtime="in_process",
                notes=["agent_command[0] not on PATH"],
            )

        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            runtime="in_process",
        )
