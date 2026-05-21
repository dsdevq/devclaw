"""Sandbox port — the contract every isolation adapter implements.

The runner does not care HOW the agent command is sandboxed (in-process subprocess, gVisor-Docker container, plain-runtime Docker container, future Firecracker microVM). It only cares about the contract: feed me a `task_id`, an `agent_command`, an `env`, a few read-only mounts and a timeout — give me back stdout, stderr, returncode, and a "did it time out?" bit. Cleanup is the adapter's problem.

See `proposals.md#2026-05-17-sandbox-adapter-choice-sandcastle` for the strategic decision behind picking Sandcastle as the first non-trivial adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

BranchStrategy = Literal["merge-to-head", "new-branch"]


@dataclass
class SandboxResult:
    """Outcome of one sandbox invocation.

    Mirrors `SubprocessResult` enough that callers can keep their JSON-on-last-line parsing logic. `runtime` is the actual runtime that ended up being used (e.g. "in_process", "runsc", "runc") — relevant because the sandcastle adapter falls back from runsc to runc when gVisor isn't installed.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    runtime: str = "unknown"
    container_name: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


class Sandbox(Protocol):
    """The single method every sandbox adapter exposes.

    `repo_url` and `branch_strategy` are passed through to the agent (via prompt or env) — the sandbox itself does not clone, the agent does. The sandbox's job is the isolation envelope, not the workflow.

    `readonly_mounts` is a map of `host_path -> container_path`. The in-process adapter ignores it (the host filesystem is already visible); container adapters bind-mount each entry read-only.

    `env` is layered ON TOP of the host environment for the in-process adapter, and is the ONLY env visible inside the container for container adapters.
    """

    def run(
        self,
        task_id: str,
        repo_url: str,
        branch_strategy: BranchStrategy,
        agent_command: list[str],
        env: dict[str, str],
        readonly_mounts: dict[str, str],
        timeout_seconds: int,
    ) -> SandboxResult: ...
