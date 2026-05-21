"""Sandbox port for per-task workdir isolation.

Today `code_task` runs the Claude Code CLI in a bare `/tmp/<task_id>` directory
inside the orchestrator container — zero isolation between concurrent tasks
beyond the unique tmpdir. A buggy or malicious edit can read/write anywhere the
container user can: `~/.claude/`, the gateway's gitconfig, other tasks' work.

This module defines a thin `Sandbox` protocol so the runner becomes
sandbox-agnostic, and provides two implementations:

  - `BareTmpdirSandbox` — current behavior; the runner just shells out into a
    fresh tmpdir. Kept as the default while the Sandcastle path bakes.
  - `SandcastleSandbox` — wraps Matt Pocock's Sandcastle
    (https://github.com/mattpocock/sandcastle), which orchestrates per-task
    Docker (or Podman / Vercel) sandboxes with bind-mount worktrees and
    branch-strategy isolation. Sandcastle itself is a Node/TypeScript library;
    we drive it through its CLI from Python.

The two implementations satisfy the same protocol so `code_task` doesn't care
which one is wired up — the choice flows in via the `sandbox` field on TaskSpec
(see `state.models`). See prior-art research at
`~/.life/tasks/2026-05-17-research-sandbox-adapter-fd88/output/findings.md`
and the proposal entry `2026-05-17-sandbox-adapter-choice-sandcastle` in
`~/.life/system/proposals.md`.

Out of scope for this module:
  - Egress allowlisting beyond Sandcastle's defaults (separate hardening pass).
  - Sandboxing `research_task` / `propose_change` / `intake` — only `code_task`
    can opt in today.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# Sandcastle is a Node/TypeScript library — we drive its CLI from Python.
# Pin the upstream npm package version here; the SandcastleSandbox implementation
# uses `npx sandcastle@<version>` so the version is reproducible without
# requiring a global install. The Python orchestrator declares this constraint
# in `pyproject.toml` under `[project.optional-dependencies] sandcastle`.
SANDCASTLE_NPM_PACKAGE = "sandcastle"
SANDCASTLE_NPM_VERSION = "0.5.2"


@dataclass
class CompletedProcess:
    """Sandbox-agnostic result of one command execution.

    Mirrors a tiny slice of `subprocess.CompletedProcess` plus a `timed_out`
    flag so callers can distinguish a hard timeout from an exit-code failure
    without re-raising.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class Sandbox(Protocol):
    """One per-task workspace. Created at runner entry, torn down at exit.

    The protocol is intentionally tiny — workdir + run + teardown — because
    every additional method is one more thing both backends have to agree on.
    Anything richer (network policy, secrets injection, snapshotting) belongs
    in the implementation, not the port.
    """

    def workdir(self) -> Path:
        """Absolute path to the task's working directory inside the sandbox.

        For `BareTmpdirSandbox` this is a real host path. For `SandcastleSandbox`
        it's the host-side bind-mount root — commands the sandbox runs see a
        possibly-different mount point internally, but the *Python* caller
        works with this host path for things like reading files that the
        sandboxed git wrote.
        """
        ...

    def run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CompletedProcess:
        """Execute `cmd` inside the sandbox. Capture stdout/stderr.

        Does NOT raise on non-zero exit; callers inspect `returncode`. Does
        NOT raise on timeout either — it returns `timed_out=True` and a
        non-zero exit. This keeps caller code branching on data, not
        exceptions.

        `cwd` defaults to `workdir()`. `env` is *added to* the inherited
        environment, not replaced (use `{"VAR": ""}` to clear a var).
        """
        ...

    def teardown(self) -> None:
        """Free anything the sandbox is holding. Safe to call twice.

        For `BareTmpdirSandbox` this removes the tmpdir. For
        `SandcastleSandbox` it tears down the Docker/Podman container and
        deletes the bind-mount root.
        """
        ...


# ─── BareTmpdirSandbox ──────────────────────────────────────────────────────


class BareTmpdirSandbox:
    """Today's behavior: `/tmp/<task_id>` with subprocess.run on the host.

    No isolation. Kept as the default for now because flipping every existing
    task onto Sandcastle in one PR is a recipe for outage-by-surprise. Tasks
    can opt in by setting `sandbox: sandcastle` on their TaskSpec; once one
    real run goes green that way, a follow-up PR flips the default.
    """

    def __init__(self, task_id: str, root: Path | None = None) -> None:
        self._task_id = task_id
        # Default to `/tmp/<task_id>` to match the legacy path layout exactly,
        # so anything that grepped for `/tmp/<task_id>/...` in logs keeps
        # working. A custom `root` exists for tests that don't want to litter
        # `/tmp`.
        base = root if root is not None else Path(tempfile.gettempdir())
        self._workdir = base / task_id
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._torn_down = False

    def workdir(self) -> Path:
        return self._workdir

    def run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CompletedProcess:
        if self._torn_down:
            raise RuntimeError(f"BareTmpdirSandbox({self._task_id}) used after teardown")

        merged_env = None
        if env is not None:
            merged_env = {**os.environ, **env}

        try:
            completed = subprocess.run(  # noqa: S603 — orchestrator runs trusted cmds
                cmd,
                cwd=str(cwd or self._workdir),
                env=merged_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout
            stderr = exc.stderr
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return CompletedProcess(
                returncode=-1,
                stdout=stdout or "",
                stderr=stderr or "",
                timed_out=True,
            )

        return CompletedProcess(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        # Be conservative — only nuke if we own the directory we created.
        if self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)


# ─── SandcastleSandbox ──────────────────────────────────────────────────────


class SandcastleNotInstalledError(RuntimeError):
    """Raised when SandcastleSandbox is constructed but `npx` is unavailable.

    Failure is loud and structured so the runner can flip the task to
    `blocked` with a precise blocker reason instead of crashing.
    """


class SandcastleSandbox:
    """Per-task Sandcastle-managed sandbox (Docker bind-mount by default).

    Sandcastle is a TypeScript library and ships as an npm package. We invoke
    it through its CLI (`npx sandcastle@<pinned>`) and treat each `run()` call
    as `sandcastle exec` against a per-task sandbox whose ID we provision in
    `__init__`. The host-side bind-mount root is our `workdir()`.

    What this buys us versus `BareTmpdirSandbox`:
      - Filesystem view in the sandbox is *only* the bind-mount; the agent
        cannot read `/home/node/.life/`, the orchestrator's git config, or
        other tasks' tmpdirs.
      - Network egress defaults to Sandcastle's allowlist (separate hardening
        pass will tighten further).
      - Branch-strategy isolation: the worktree the agent edits is *not* the
        host repo's worktree, so a malformed git op can't corrupt host state.

    What it does NOT buy:
      - Strong kernel-attack-surface isolation. The research note recommends
        layering gVisor's `runsc` as the Docker runtime on the host for that.
        Not in scope for this PR.

    Notes for callers:
      - `workdir()` is the host path. `run()` translates `cwd` from host paths
        to sandbox-internal paths automatically when `cwd` is rooted at
        `workdir()`.
      - Network egress: defaults to Sandcastle defaults — see follow-up
        hardening task for a stricter allowlist.
    """

    # Path inside the Sandcastle container where the bind-mount lands.
    # Sandcastle's docker-provider default is `/workspace`; we hardcode it
    # rather than parse Sandcastle's output to keep the wrapper simple.
    _CONTAINER_WORKDIR = "/workspace"

    def __init__(
        self,
        task_id: str,
        root: Path | None = None,
        *,
        npm_package: str = SANDCASTLE_NPM_PACKAGE,
        npm_version: str = SANDCASTLE_NPM_VERSION,
        provider: Literal["docker", "podman"] = "docker",
        npx_bin: str | None = None,
    ) -> None:
        self._task_id = task_id
        self._npm_spec = f"{npm_package}@{npm_version}"
        self._provider = provider
        self._npx_bin = npx_bin or shutil.which("npx") or "npx"

        if shutil.which(self._npx_bin) is None:
            raise SandcastleNotInstalledError(
                f"`{self._npx_bin}` not found on PATH — Sandcastle requires a Node.js "
                "toolchain on the orchestrator host. Install Node 20+ or fall back to "
                "the bare tmpdir sandbox by setting `sandbox: bare`."
            )

        base = root if root is not None else Path(tempfile.gettempdir())
        self._workdir = base / task_id
        self._workdir.mkdir(parents=True, exist_ok=True)

        # Sandcastle identifies a sandbox by name. Reuse the task_id so logs
        # are greppable across host and sandbox sides.
        self._sandbox_name = f"devclaw-{task_id}"
        self._torn_down = False
        self._created = False
        self._create()

    def workdir(self) -> Path:
        return self._workdir

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _create(self) -> None:
        """Provision the Sandcastle sandbox. Idempotent.

        We invoke `npx sandcastle create --name <task_id> --provider <p> --bind <host>`
        once; if the upstream CLI surface changes, this is the one place to
        adapt the call shape.
        """
        cmd = [
            self._npx_bin,
            "--yes",
            self._npm_spec,
            "create",
            "--name",
            self._sandbox_name,
            "--provider",
            self._provider,
            "--bind",
            f"{self._workdir}:{self._CONTAINER_WORKDIR}",
        ]
        logger.info("sandcastle: provisioning sandbox %s", self._sandbox_name)
        completed = subprocess.run(  # noqa: S603 — invoking pinned npm CLI
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # Surface upstream's error so blockers are actionable. We do NOT
            # swallow into a generic message — operators need the CLI tail.
            raise SandcastleNotInstalledError(
                f"sandcastle create failed (exit {completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip() or '<empty>'}"
            )
        self._created = True

    def run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CompletedProcess:
        if self._torn_down:
            raise RuntimeError(f"SandcastleSandbox({self._task_id}) used after teardown")
        if not self._created:
            raise RuntimeError(
                f"SandcastleSandbox({self._task_id}) used before successful create"
            )

        # Translate host-side `cwd` into the container path. We only know how
        # to do this for paths rooted at `workdir()`; anything else is a bug
        # in the caller because the sandbox can't see arbitrary host paths.
        container_cwd = self._CONTAINER_WORKDIR
        if cwd is not None:
            try:
                rel = Path(cwd).resolve().relative_to(self._workdir.resolve())
                container_cwd = str(Path(self._CONTAINER_WORKDIR) / rel)
            except ValueError:
                raise ValueError(
                    f"cwd={cwd!r} is outside the sandbox workdir ({self._workdir!r}); "
                    "SandcastleSandbox cannot see arbitrary host paths"
                ) from None

        exec_cmd = [
            self._npx_bin,
            "--yes",
            self._npm_spec,
            "exec",
            "--name",
            self._sandbox_name,
            "--cwd",
            container_cwd,
        ]
        if env:
            for key, val in env.items():
                exec_cmd.extend(["--env", f"{key}={val}"])
        exec_cmd.append("--")
        exec_cmd.extend(cmd)

        try:
            completed = subprocess.run(  # noqa: S603 — invoking pinned npm CLI
                exec_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout
            stderr = exc.stderr
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return CompletedProcess(
                returncode=-1,
                stdout=stdout or "",
                stderr=stderr or "",
                timed_out=True,
            )

        return CompletedProcess(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        if self._created:
            destroy_cmd = [
                self._npx_bin,
                "--yes",
                self._npm_spec,
                "destroy",
                "--name",
                self._sandbox_name,
            ]
            subprocess.run(  # noqa: S603 — invoking pinned npm CLI
                destroy_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        if self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)


# ─── factory ────────────────────────────────────────────────────────────────


def make_sandbox(
    task_id: str,
    kind: Literal["bare", "sandcastle"] = "bare",
    *,
    root: Path | None = None,
) -> Sandbox:
    """Construct the Sandbox implementation named by `kind`.

    Centralized here so `code_task` doesn't import every implementation, and
    so future kinds (`gvisor`, `firecracker`, ...) get added in one place.
    """
    if kind == "bare":
        return BareTmpdirSandbox(task_id, root=root)
    if kind == "sandcastle":
        return SandcastleSandbox(task_id, root=root)
    raise ValueError(f"unknown sandbox kind: {kind!r} (expected 'bare' or 'sandcastle')")


__all__ = [
    "BareTmpdirSandbox",
    "CompletedProcess",
    "Sandbox",
    "SandcastleNotInstalledError",
    "SandcastleSandbox",
    "SANDCASTLE_NPM_PACKAGE",
    "SANDCASTLE_NPM_VERSION",
    "make_sandbox",
]
