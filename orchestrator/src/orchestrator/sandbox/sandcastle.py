"""Sandcastle adapter — ephemeral per-task Docker containers via mattpocock/sandcastle.

Each `run()` call spins up exactly one container, runs the agent inside it, captures stdout/stderr/returncode, and removes the container. The container's workdir lives inside the container and is destroyed with it — that is what closes the "no per-task sandbox isolation" gap AND the disk-bloat issue (~/.life/journal/2026-05-20.md: orchestrator /tmp accumulating ~1.2GB of task clones).

## Runtime selection — runsc with graceful fallback

Strategy (from proposals.md#2026-05-17-sandbox-adapter-choice-sandcastle):

  1. **Preferred:** gVisor (`--runtime=runsc`). User-space kernel re-implementation; mid-strength isolation; per-task workdir is fully ephemeral.
  2. **Fallback:** default Docker runtime (`runc`). Process isolation only — same boundary as today's daemon, but at least the workdir is still per-task ephemeral, which is the disk-bloat win independent of the isolation win.

The fallback is engaged automatically when `runsc` is not registered with the local Docker daemon. This matters because:

  - Phase 1 (host install) is driven interactively by Denys/Kit; if a sweep tick lands BEFORE Phase 1 is complete, the adapter must degrade rather than wedge.
  - On ARM64 about ~18% of syscalls are unimplemented in gVisor; node / git / gh / claude may hit one of them. The fallback gives us a no-touch escape hatch (see risk register in PR body) — operators just remove `runsc` from `/etc/docker/daemon.json` and the adapter notices on next invocation.

The fallback emits a WARN log every time it engages, so we can spot a stuck fallback in production logs.

## Sandcastle CLI

We shell out via `npx -y sandcastle@<pinned-version> ...`. The pinned version lives in `orchestrator/src/orchestrator/config/sandbox.yaml` (`sandcastle.version`), NOT in this file — flag drift is a known risk in sandcastle's pre-1.0 phase, so we want one place to bump.

The orchestrator's `Dockerfile` runs `npx -y sandcastle@<pinned> --help` once at build time so first-task latency does not include the ~30s npm tarball pull. See `orchestrator/Dockerfile`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass

from orchestrator.sandbox.base import BranchStrategy, SandboxResult

logger = logging.getLogger(__name__)

# Default image — overridable via sandbox.yaml `sandcastle.image`. Must contain git, gh, node, claude CLI.
DEFAULT_IMAGE = "lifekit-openclaw:local"

# Sandcastle version known to work with this adapter. Bumping requires a re-test of the smoke (see acceptance criteria).
DEFAULT_SANDCASTLE_VERSION = "0.5.2"


@dataclass
class SandcastleConfig:
    """Resolved configuration for one Sandcastle adapter instance.

    Built from `config/sandbox.yaml` at adapter-construction time. Keeping it in a frozen dataclass means we don't re-read yaml on the hot path AND tests can inject a config without touching disk.
    """

    image: str = DEFAULT_IMAGE
    version: str = DEFAULT_SANDCASTLE_VERSION
    runtime: str = "runsc"  # "runsc" | "runc"
    fallback_runtime: str = "runc"
    npx_bin: str = "npx"


class SandcastleSandbox:
    """The Sandcastle Sandbox port adapter."""

    name = "sandcastle"

    def __init__(
        self,
        config: SandcastleConfig | None = None,
        *,
        runtime_probe: "RuntimeProbe | None" = None,
    ) -> None:
        self.config = config or SandcastleConfig()
        # `runtime_probe` is injectable so the tests can stub gVisor presence without needing a real Docker daemon. Production code uses the default `DockerRuntimeProbe`.
        self._probe = runtime_probe or DockerRuntimeProbe()

    # ── public API ──────────────────────────────────────────────────────────

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
        runtime, fallback_notes = self._select_runtime()
        container_name = self.container_name_for(task_id)

        cmd = self._build_sandcastle_cmd(
            container_name=container_name,
            runtime=runtime,
            agent_command=agent_command,
            env=env,
            readonly_mounts=readonly_mounts,
            timeout_seconds=timeout_seconds,
        )

        logger.info(
            "sandcastle.run task_id=%s runtime=%s container=%s image=%s",
            task_id,
            runtime,
            container_name,
            self.config.image,
        )

        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 30,  # give sandcastle a small overhead margin
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial_stdout = exc.stdout
            partial_stderr = exc.stderr
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode("utf-8", errors="replace")
            self._cleanup(container_name)
            return SandboxResult(
                returncode=-1,
                stdout=partial_stdout or "",
                stderr=partial_stderr or "",
                timed_out=True,
                runtime=runtime,
                container_name=container_name,
                notes=fallback_notes + [f"timeout after {timeout_seconds}s"],
            )
        except FileNotFoundError as exc:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"sandcastle/npx not on PATH: {exc}",
                runtime=runtime,
                container_name=container_name,
                notes=fallback_notes + ["npx_or_sandcastle_missing"],
            )

        if completed.returncode != 0:
            # Sandcastle / Docker error path — always attempt cleanup even on success-via-cleanup races. `docker rm` is a no-op if the container already self-cleaned.
            self._cleanup(container_name)

        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            runtime=runtime,
            container_name=container_name,
            notes=fallback_notes,
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def container_name_for(task_id: str) -> str:
        """Deterministic-prefix container name.

        Prefix `devclaw-task-` is what `docker ps --filter name=devclaw-task-` greps for. UUID suffix keeps names collision-free if a task is retried within one Docker daemon lifecycle.
        """
        # Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]+. Some task_ids contain characters we should strip.
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in task_id)
        return f"devclaw-task-{safe}-{uuid.uuid4().hex[:8]}"

    def _select_runtime(self) -> tuple[str, list[str]]:
        """Return (chosen_runtime, notes). Notes captures fallback events for the SandboxResult."""
        preferred = self.config.runtime
        if preferred == self.config.fallback_runtime:
            return preferred, []
        if self._probe.is_available(preferred):
            return preferred, []
        msg = (
            f"runtime={preferred!r} not registered with Docker; "
            f"falling back to {self.config.fallback_runtime!r} (process isolation only)"
        )
        logger.warning("sandcastle.fallback %s", msg)
        return self.config.fallback_runtime, [msg]

    def _build_sandcastle_cmd(
        self,
        *,
        container_name: str,
        runtime: str,
        agent_command: list[str],
        env: dict[str, str],
        readonly_mounts: dict[str, str],
        timeout_seconds: int,
    ) -> list[str]:
        cmd: list[str] = [
            self.config.npx_bin,
            "-y",
            f"sandcastle@{self.config.version}",
            "run",
            "--image",
            self.config.image,
            "--name",
            container_name,
            "--runtime",
            runtime,
            "--timeout",
            str(timeout_seconds),
            "--rm",  # auto-remove on exit; we still call docker rm in _cleanup as belt-and-braces
        ]
        for key, value in sorted(env.items()):
            cmd.extend(["--env", f"{key}={value}"])
        for host_path, container_path in sorted(readonly_mounts.items()):
            cmd.extend(["--mount", f"{host_path}:{container_path}:ro"])
        cmd.append("--")
        cmd.extend(agent_command)
        return cmd

    def _cleanup(self, container_name: str) -> None:
        """Force-remove the container if `--rm` didn't already.

        Swallows errors — if the container is already gone (the happy path), `docker rm` returns non-zero and we don't care. The only failure we'd want to surface is "docker daemon unreachable", which the next `run()` will surface anyway.
        """
        docker = shutil.which("docker")
        if docker is None:
            logger.debug("sandcastle.cleanup skipped: docker not on PATH")
            return
        try:
            subprocess.run(
                [docker, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            logger.debug("sandcastle.cleanup failed for %s: %s", container_name, exc)


# ── runtime probe ───────────────────────────────────────────────────────────


class RuntimeProbe:
    """Protocol-shaped helper for checking whether a Docker runtime is available.

    Pulled out so tests can swap in a stub without needing a Docker daemon.
    """

    def is_available(self, runtime: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class DockerRuntimeProbe(RuntimeProbe):
    """Real runtime probe — asks `docker info` for registered runtimes."""

    def is_available(self, runtime: str) -> bool:
        docker = shutil.which("docker")
        if docker is None:
            return False
        try:
            completed = subprocess.run(
                [docker, "info", "--format", "{{json .Runtimes}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={**os.environ},
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
        if completed.returncode != 0:
            return False
        # Cheap substring check beats parsing JSON — registered runtimes appear as `"runsc":{...}` in the output.
        return f'"{runtime}"' in (completed.stdout or "")
