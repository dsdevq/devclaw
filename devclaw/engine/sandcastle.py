"""Per-task docker sandbox runner — the OpenHands :class:`~devclaw.engine.Engine`.

This is the one concrete Engine implementation (see ``engine.py`` for the seam).
Spawns ``docker run --rm`` against the devclaw-sandbox image for each task. The
container's ENTRYPOINT runs the OpenHands runner (``openhands-runner/runner.py``),
which streams one prefixed JSON line per event (``event: {...}``) plus a single
terminating ``result: {...}`` line. This module:

  - Translates an ``EngineRequest`` into a docker invocation.
  - Bind-mounts the host workspace into /workspace and a CURATED allowlist of
    entries under ~/.claude (default: just the OAuth credential) read-only into
    /home/agent/.claude — auth in, the host's personal skills/plugins/MCP/global
    CLAUDE.md out (Pro OAuth posture: read tokens, don't write back). See
    ``SANDBOX_CLAUDE_ALLOWLIST``.
  - Streams stdout line-by-line; routes ``event:`` lines through ``on_event``
    and parses the final ``result:`` line as the result.
  - Refuses to forward ANTHROPIC_API_KEY into the container (same belt +
    suspenders the runner enforces).

Container lifecycle: --rm + the per-task --name make destroy-on-exit automatic;
no persistent on-host state. But --rm dies with its own docker CLI process — if
the devclaw process is killed mid-task, the container keeps running with nothing
left to reap it. Every sandbox therefore also carries the ``devclaw.sandbox=1``
label, and :func:`sweep_orphan_sandboxes` reaps leftovers at the next startup
(wired into ``TaskQueue.recover``). Tests inject a stub runner (via TaskQueue's
``runner`` param) so they don't need docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

from . import EngineRequest, EngineResult
from .runner_io import STREAM_LINE_LIMIT, consume_runner_output

SANDBOX_IMAGE = os.environ.get("DEVCLAW_SANDBOX_IMAGE", "devclaw-sandbox:latest")
DOCKER_BIN = os.environ.get("DEVCLAW_DOCKER_BIN", "docker")
# The model the in-sandbox OpenHands agent runs on — this is the heavy coding
# path and the bulk of the Pro/Max quota burn, so it defaults to Sonnet (strong
# at code, far lighter than Opus); set DEVCLAW_EXEC_MODEL=claude-opus-4-8 to opt
# a run up to Opus. Passed to the runner, which hands it to ACPAgent as the
# `acp_model` (Claude ACP selects it via session _meta). Must be a full model id,
# not an alias. Empty → the ACP server's default.
EXEC_MODEL = os.environ.get("DEVCLAW_EXEC_MODEL", "claude-sonnet-4-6") or None
# Per-sandbox resource caps. The task queue bounds the NUMBER of concurrent
# builds (DEVCLAW_MAX_CONCURRENT), but without a per-container memory ceiling N
# parallel builds can still OOM a small VPS. --memory-swap == --memory disables
# swap growth (a hard ceiling). Generous by default (builds run pip/compilers +
# claude); tighten per host via env.
SANDBOX_MEMORY = os.environ.get("DEVCLAW_SANDBOX_MEMORY", "2g")
SANDBOX_CPUS = os.environ.get("DEVCLAW_SANDBOX_CPUS", "2.0")
# The identity label every task sandbox carries, and the ONLY filter the startup
# orphan sweep matches. Container names (devclaw-<uuid8>) are never persisted, so
# after a process death the label is the one durable handle on leaked sandboxes.
# Deploy containers use `devclaw.deploy=1` (delivery/deploy.py) — a deliberately
# different label, outside the sweep's scope.
SANDBOX_LABEL = "devclaw.sandbox=1"
# Upper bound (seconds) on the teardown reaper's `docker rm -f` wait. Teardown
# exists to enforce the task wall-clock timeout, but asyncio.wait_for waits for
# the cancelled coroutine's cleanup before raising — so an UNbounded reaper wait
# against a wedged docker daemon would defeat the very timeout it serves. On
# expiry we log one line and move on: the container may leak until the next
# startup sweep, but the orchestrator never hangs.
TEARDOWN_TIMEOUT_S = float(os.environ.get("DEVCLAW_TEARDOWN_TIMEOUT_S", "30"))
# Per-call cap for the synchronous docker CLI calls in the startup sweep — the
# sweep runs before the server serves, so it must be bounded too.
SWEEP_DOCKER_TIMEOUT_S = 10.0
# Container-side mount targets. Match the Dockerfile's expectations.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CLAUDE_DIR = "/home/agent/.claude"

# Which entries under the host ~/.claude get bound into the sandbox config dir.
# Default: the OAuth *identity pair* — `.credentials.json` (the token) AND
# `.claude.json` (the account identity: oauthAccount + userID). Both are needed:
# `claude --print` authenticates with the credential alone, but the ACP *agentic*
# loop hangs after init without `.claude.json` (it needs the account identity to
# act, not just the token — auth != agency; this was a live-found regression when
# the default was credential-only). `.claude.json` here carries identity + caches,
# NOT the leak (no mcpServers, projects empty). We still deliberately do NOT mount
# the whole host ~/.claude: that dir also holds skills/, plugins/ (+ their MCP
# servers that need absent network/auth), the global CLAUDE.md (which points at the
# unmounted ~/memory, so its instructions are dead in here), and projects/ +
# history — projecting all of that into the engineer is non-reproducible and full
# of tools that fail or mislead. The PM hands the engineer a curated toolbox, not
# the keys to the whole house. Add entries (relative to ~/.claude) via
# DEVCLAW_SANDBOX_CLAUDE_ALLOWLIST only with intent; they must exist on the host —
# we don't stat (the host path is invisible when devclaw itself runs containerized)
# so a missing entry surfaces as a docker bind error, not a silent skip.
_DEFAULT_CLAUDE_ALLOWLIST = (".credentials.json", ".claude.json")
SANDBOX_CLAUDE_ALLOWLIST: tuple[str, ...] = tuple(
    e.strip()
    for e in os.environ.get("DEVCLAW_SANDBOX_CLAUDE_ALLOWLIST", "").split(",")
    if e.strip()
) or _DEFAULT_CLAUDE_ALLOWLIST


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
        # Bounded — an unbounded wait here would let a wedged docker daemon hang
        # the reap forever and defeat the task wall-clock timeout that teardown
        # exists to enforce (asyncio.wait_for waits for the cancelled coroutine's
        # cleanup — i.e. this function — before raising). See TEARDOWN_TIMEOUT_S.
        await asyncio.wait_for(killer.wait(), timeout=TEARDOWN_TIMEOUT_S)
    except asyncio.TimeoutError:
        sys.stderr.write(
            f"sandcastle-runner: reap of {container_name} timed out after "
            f"{TEARDOWN_TIMEOUT_S}s — daemon wedged? Leaving it to the next "
            f"startup sweep.\n"
        )
    except asyncio.CancelledError:
        pass
    except Exception as err:  # pragma: no cover - defensive
        sys.stderr.write(
            f"sandcastle-runner: force-remove of {container_name} failed: {err}\n"
        )


def _docker_run_sync(args: list[str]) -> "subprocess.CompletedProcess[str]":
    """One synchronous, bounded docker CLI call — the sweep's subprocess seam
    (tests patch this, mirroring ``deploy.py``'s ``_run``)."""
    return subprocess.run(
        [DOCKER_BIN, *args],
        capture_output=True,
        text=True,
        timeout=SWEEP_DOCKER_TIMEOUT_S,
    )


def sweep_orphan_sandboxes() -> int:
    """Reap task-sandbox containers leaked by a previous devclaw process.

    ``--rm`` only fires when its own ``docker run`` client exits, so a devclaw
    process that dies mid-task leaves the container running with nothing to reap
    it — while crash recovery resets the DB row and re-runs the task in a SECOND
    container, the original burns quota and memory forever. This sweeps by the
    ``devclaw.sandbox=1`` label (the name is never persisted); at startup —
    before this process has launched anything — every labeled container is by
    definition orphaned, since sandboxes don't legitimately outlive their
    launching process. Deploy containers (``devclaw.deploy=1``) are out of scope.

    Synchronous (call before serving), best-effort: returns the number of
    containers removed, 0 when docker is unavailable (host/stub engine
    environments, CI) — never raises.
    """
    try:
        ps = _docker_run_sync(["ps", "-q", "--filter", f"label={SANDBOX_LABEL}"])
    except (OSError, subprocess.SubprocessError):
        return 0  # docker missing/unreachable/slow — nothing to sweep here
    if ps.returncode != 0:
        return 0
    reaped = 0
    for cid in (line.strip() for line in ps.stdout.splitlines()):
        if not cid:
            continue
        try:
            rm = _docker_run_sync(["rm", "-f", cid])
        except (OSError, subprocess.SubprocessError):
            continue
        if rm.returncode == 0:
            reaped += 1
    return reaped


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


def _validate_workspace(workspace_dir: str) -> str | None:
    """Catch the silent-timeout trap: an upstream that hands us a workspace_dir
    we can't usefully bind-mount as ``/workspace``. Returns an error message if
    the workspace is unusable, ``None`` otherwise.

    Two failure modes are silent without this gate:

    1. **Out-of-prefix path** — when devclaw runs containerized, only paths
       under ``DEVCLAW_CONTAINER_PATH_PREFIX`` translate to a host path the
       sibling sandbox can bind. A foreign path (e.g. an openclaw-waiter-side
       tmp dir) passes through ``_translate_workspace_path`` unchanged and
       docker mounts whatever happens to exist at that host location — usually
       nothing, an empty dir, or stale content.
    2. **Empty bind source** — even an in-prefix path may have been wiped or
       never populated. An empty bind-mount looks identical to a hung sandbox:
       the agent enters ``/workspace``, finds no repo, can't make progress,
       and burns the full wall-clock before being torn down. The planner sees
       only a generic timeout and has to guess.

    Fail fast with a specific message instead — the goal layer surfaces it
    verbatim and the operator (or planner) can correct course immediately."""
    container_prefix = os.environ.get("DEVCLAW_CONTAINER_PATH_PREFIX")
    host_prefix = os.environ.get("DEVCLAW_HOST_PATH_PREFIX")
    if container_prefix and host_prefix and not workspace_dir.startswith(container_prefix):
        return (
            f"workspace_dir {workspace_dir!r} is outside the devclaw workspaces "
            f"mount ({container_prefix!r} → {host_prefix!r}). The sibling sandbox "
            f"cannot bind-mount paths it doesn't own; pass a workspace under "
            f"{container_prefix}."
        )
    # Check the workspace AS THE devclaw PROCESS SEES IT — same dir contents as
    # the host bind source (the container_prefix mount points at host_prefix).
    # In local-dev (no prefix translation) this is also the host path itself.
    p = Path(workspace_dir)
    if not p.exists():
        return (
            f"workspace_dir {workspace_dir!r} does not exist. The sandbox would "
            f"mount a non-existent path as /workspace and time out with no signal."
        )
    if p.is_dir():
        try:
            next(p.iterdir())
        except StopIteration:
            return (
                f"workspace_dir {workspace_dir!r} is an EMPTY directory. The "
                f"sandbox would mount it as an empty /workspace, the agent "
                f"would find no repo, and the run would time out at the "
                f"wall-clock. Clone or restore the workspace first."
            )
        except (PermissionError, OSError):
            # Can't stat — fall through and let docker speak for itself rather
            # than refusing a possibly-valid path.
            pass
    return None


def _build_claude_mounts(claude_dir: str, allowlist: tuple[str, ...]) -> list[str]:
    """``-v`` args binding ONLY the allowlisted entries under the host ~/.claude
    into the sandbox config dir, each read-only. The curated boundary: auth in,
    the rest of the host's personal Claude setup out. See ``SANDBOX_CLAUDE_ALLOWLIST``
    for the rationale."""
    base = claude_dir.rstrip("/")
    args: list[str] = []
    for rel in allowlist:
        rel = rel.strip("/")
        args += ["-v", f"{base}/{rel}:{CONTAINER_CLAUDE_DIR}/{rel}:ro"]
    return args


def _build_docker_args(
    *,
    container_name: str,
    host_bind_path: str,
    claude_dir: str,
    payload: str,
    allowlist: tuple[str, ...] = SANDBOX_CLAUDE_ALLOWLIST,
) -> list[str]:
    """Assemble the full ``docker run`` argv for one task. Pure (no I/O) so the
    mount posture — curated claude allowlist, writable scratch tmpfs, no API-key
    leak, host networking — is unit-testable without docker."""
    return [
        "run",
        "--rm",
        "--name",
        container_name,
        # Durable identity for the startup orphan sweep: the name above is never
        # persisted and --rm dies with the docker CLI, so this label is the only
        # handle on a sandbox whose devclaw process crashed mid-task.
        "--label",
        SANDBOX_LABEL,
        "--network",
        "host",  # claude OAuth refresh needs egress; tighten later via allowlist.
        # Per-build resource ceiling so N concurrent sandboxes can't OOM the VPS.
        "--memory", SANDBOX_MEMORY, "--memory-swap", SANDBOX_MEMORY,
        "--cpus", SANDBOX_CPUS,
        "-v",
        f"{host_bind_path}:{CONTAINER_WORKSPACE}",
        # Curated claude config: only the allowlisted auth, read-only (NOT the whole
        # host ~/.claude — see SANDBOX_CLAUDE_ALLOWLIST).
        *_build_claude_mounts(claude_dir, allowlist),
        # The config dir is non-writable (RO binds), but the claude CLI must write
        # per-session scratch *under* it — `session-env/<uuid>` (a working dir per
        # shell session) + `shell-snapshots/`. On the RO mount those mkdirs hit
        # EROFS, which breaks EVERY terminal tool call the agent makes. Overlay just
        # those two subpaths with a writable tmpfs — auth stays RO, scratch becomes
        # writable. (Verified: claude auths + runs with only the credential present
        # and the config root non-writable, so this scratch overlay is all it needs.)
        "--tmpfs",
        f"{CONTAINER_CLAUDE_DIR}/session-env:rw,exec",
        "--tmpfs",
        f"{CONTAINER_CLAUDE_DIR}/shell-snapshots:rw,exec",
        "-e",
        "OPENHANDS_SUPPRESS_BANNER=1",
        SANDBOX_IMAGE,
        payload,
    ]


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
    # Fail fast on a workspace the sandbox can't usefully mount — see
    # _validate_workspace for the two silent-timeout traps this closes.
    bind_err = _validate_workspace(req.workspace_dir)
    if bind_err is not None:
        return {"status": "error", "error": bind_err}
    host_bind_path = _translate_workspace_path(req.workspace_dir)

    # Per-task container name for greppable logs + manual cleanup if --rm fails.
    container_name = f"devclaw-{uuid.uuid4().hex[:8]}"

    payload = json.dumps(
        {
            "kind": req.kind,
            "workspace_dir": CONTAINER_WORKSPACE,
            "goal": req.goal,
            "model": EXEC_MODEL,  # the in-sandbox agent's tier; None → ACP default
            # verify gate runs INSIDE the container after the agent finishes —
            # same toolchain + workspace the agent built in (None → no gate).
            "verify_cmd": req.verify_cmd,
        }
    )

    docker_args = _build_docker_args(
        container_name=container_name,
        host_bind_path=host_bind_path,
        claude_dir=claude_dir,
        payload=payload,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN,
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # large per-line buffer — a single event can exceed the 64 KiB default
            # (big diffs / file observations); see STREAM_LINE_LIMIT.
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
        return await consume_runner_output(proc, req.on_event, label="sandbox")
    finally:
        # On cancellation the read above raises CancelledError straight into
        # here with the container still alive — tear it down (docker-specific,
        # so it can't live in the engine-agnostic reader). On a clean exit proc
        # has already returned, so teardown is a cheap no-op.
        if proc.returncode is None:
            await _teardown(proc, container_name)
