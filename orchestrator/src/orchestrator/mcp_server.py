"""devclaw-mcp — MCP server bridging OpenClaw (or any MCP caller) to the
devclaw orchestrator.

Two execution modes, selected by DEVCLAW_LOCAL env var:

  Local (DEVCLAW_LOCAL=1, default inside the devclaw-mcp container):
    Each tool call runs `devclaw-orchestrator <subcommand>` as a local
    subprocess. No SSH. DEVCLAW_LIFE_DIR sets the ~/.life path (default: the
    LIFEKIT_ROOT env var, or ~/.life).

  Remote (default, for PC-side use):
    Each tool call SSH-es to the VPS as DEVCLAW_VPS_USER@DEVCLAW_VPS_HOST
    and runs the same subcommand remotely. No new ports, auth via Tailscale.

Config (remote mode):
  DEVCLAW_VPS_HOST  — VPS hostname (default: "lifekit-vps")
  DEVCLAW_VPS_USER  — SSH user (default: "lifekit")
  DEVCLAW_SSH_BIN   — ssh binary (default: "ssh"; override in tests)

Config (local mode):
  DEVCLAW_LOCAL     — set to "1" to enable local mode
  DEVCLAW_LIFE_DIR  — ~/.life path (default: LIFEKIT_ROOT env, or ~/.life)

Transport:
  stdio (default) — standard MCP local-tool transport for PC use
  http            — streamable-http for VPS container use (start with --transport http)
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SSH_TIMEOUT_SECONDS = 60
INTAKE_TIMEOUT_SECONDS = 600

_LOCAL_MODE: bool = os.environ.get("DEVCLAW_LOCAL", "0").lower() in ("1", "true", "yes")
_LIFE_DIR: str = os.environ.get(
    "DEVCLAW_LIFE_DIR",
    os.environ.get("LIFEKIT_ROOT", os.path.expanduser("~/.life")),
)


# ─── execution backends ──────────────────────────────────────────────────────


@dataclass
class SshConfig:
    host: str
    user: str
    ssh_bin: str = "ssh"

    @classmethod
    def from_env(cls) -> SshConfig:
        return cls(
            host=os.environ.get("DEVCLAW_VPS_HOST", "lifekit-vps"),
            user=os.environ.get("DEVCLAW_VPS_USER", "lifekit"),
            ssh_bin=os.environ.get("DEVCLAW_SSH_BIN", "ssh"),
        )


def _ssh_argv(cfg: SshConfig, remote_cmd: str) -> list[str]:
    return [
        cfg.ssh_bin,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg.user}@{cfg.host}",
        remote_cmd,
    ]


def _run_ssh(
    cfg: SshConfig,
    remote_cmd: str,
    *,
    stdin_text: str | None = None,
    timeout: int = SSH_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    argv = _ssh_argv(cfg, remote_cmd)
    proc = subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_local(
    cmd_parts: list[str],
    *,
    stdin_text: str | None = None,
    timeout: int = SSH_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd_parts,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_cmd(
    cmd_parts: list[str],
    *,
    stdin_text: str | None = None,
    timeout: int = SSH_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Dispatch to local subprocess or SSH depending on DEVCLAW_LOCAL."""
    if _LOCAL_MODE:
        return _run_local(cmd_parts, stdin_text=stdin_text, timeout=timeout)
    cfg = SshConfig.from_env()
    remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    return _run_ssh(cfg, remote_cmd, stdin_text=stdin_text, timeout=timeout)


def _ssh_error(exc: Exception, *, action: str) -> dict[str, Any]:
    return {
        "error": f"ssh_failed_{action}",
        "detail": f"{type(exc).__name__}: {exc}",
    }


def _last_json_line(stdout: str, stderr: str, *, action: str) -> dict[str, Any]:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {"error": f"{action}_no_output", "detail": stderr.strip()}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"error": f"{action}_bad_json", "detail": f"{exc}: {lines[-1][:200]}"}


# ─── tools ───────────────────────────────────────────────────────────────────


def devclaw_intake(prose: str, from_surface: str = "openclaw") -> dict[str, Any]:
    """File a task against the devclaw orchestrator.

    Converts a natural-language intent into a TaskSpec on disk and returns
    {task_id, spec_path, budget_min, target_repo, state} where state is
    "new" or "duplicate". Idempotent on (prose, from_surface).
    """
    cmd = [
        "devclaw-orchestrator", "intake",
        "--from", from_surface,
        "--life", _LIFE_DIR,
    ]
    try:
        rc, stdout, stderr = _run_cmd(cmd, stdin_text=prose, timeout=INTAKE_TIMEOUT_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_intake failed: %s", exc)
        return _ssh_error(exc, action="intake")
    if rc != 0:
        return {"error": "intake_failed", "detail": stderr.strip() or stdout.strip() or f"rc={rc}"}
    return _last_json_line(stdout, stderr, action="intake")


def devclaw_status(task_id: str) -> dict[str, Any]:
    """Look up the current state of a task by task_id.

    Returns {state, last_action, pr_url?, completed_at?, blocker?, ...}.
    state is one of: ready, dispatched-*, done, blocked, unknown.
    """
    cmd = ["devclaw-orchestrator", "status", task_id, "--life", _LIFE_DIR]
    try:
        rc, stdout, stderr = _run_cmd(cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_status failed: %s", exc)
        return _ssh_error(exc, action="status")
    if rc != 0:
        return {"error": "status_failed", "detail": stderr.strip() or stdout.strip() or f"rc={rc}"}
    return _last_json_line(stdout, stderr, action="status")


def devclaw_list(limit: int = 20, status: str | None = None) -> dict[str, Any]:
    """List recent devclaw tasks from the run history.

    Returns {tasks: [...]} where each task has ts, task_id, kind, target_repo,
    status, duration_seconds, retries, pr_url. Useful for "what's going on"
    queries. Filter by status: done | failed | watchdog_killed.
    """
    cmd = ["devclaw-orchestrator", "runs", "tail", "--json", "--limit", str(limit)]
    if status:
        cmd += ["--status", status]
    try:
        rc, stdout, stderr = _run_cmd(cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_list failed: %s", exc)
        return _ssh_error(exc, action="list")
    if rc != 0:
        return {"error": "list_failed", "detail": stderr.strip() or stdout.strip() or f"rc={rc}"}
    tasks = [json.loads(ln) for ln in stdout.splitlines() if ln.strip()]
    return {"tasks": tasks}


def devclaw_logs(task_id: str) -> dict[str, Any]:
    """Get full context for a task — intent, acceptance criteria, result, blocker.

    Returns everything needed to understand what a task was trying to do and
    why it succeeded or failed. Use this when a task is blocked or failed and
    you need context to help solve the problem.
    """
    cmd = ["devclaw-orchestrator", "logs", task_id, "--life", _LIFE_DIR]
    try:
        rc, stdout, stderr = _run_cmd(cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_logs failed: %s", exc)
        return _ssh_error(exc, action="logs")
    if rc != 0:
        return {"error": "logs_failed", "detail": stderr.strip() or stdout.strip() or f"rc={rc}"}
    return _last_json_line(stdout, stderr, action="logs")


def devclaw_unblock(task_id: str, decision: str) -> dict[str, Any]:
    """Provide a decision to unblock a blocked task and re-queue it.

    Pass the task_id of the blocked task and a decision string (the user's
    instruction for how to proceed). The orchestrator will pick it up on the
    next sweep tick and re-dispatch the task with the decision attached.

    Returns {task_id, state, decision_written} on success.
    """
    cmd = [
        "devclaw-orchestrator", "unblock", task_id,
        "--decision", decision,
        "--life", _LIFE_DIR,
    ]
    try:
        rc, stdout, stderr = _run_cmd(cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_unblock failed: %s", exc)
        return _ssh_error(exc, action="unblock")
    if rc != 0:
        return {"error": "unblock_failed", "detail": stderr.strip() or stdout.strip() or f"rc={rc}"}
    return _last_json_line(stdout, stderr, action="unblock")


# ─── MCP server wiring ───────────────────────────────────────────────────────


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("devclaw")

    @server.tool(
        name="devclaw_intake",
        description=(
            "File a task against the devclaw orchestrator. Pass a natural-language "
            "intent as `prose`. `from_surface` labels the provenance (default: 'openclaw'). "
            "Returns {task_id, spec_path, budget_min, target_repo, state} where state "
            "is 'new' or 'duplicate'. Idempotent on (prose, from_surface)."
        ),
    )
    def _intake_tool(prose: str, from_surface: str = "openclaw") -> dict[str, Any]:
        return devclaw_intake(prose=prose, from_surface=from_surface)

    @server.tool(
        name="devclaw_status",
        description=(
            "Look up a task's current state by task_id. Returns {state, last_action, "
            "pr_url?, completed_at?, blocker?}. state is one of: ready, dispatched-*, "
            "done, blocked, unknown."
        ),
    )
    def _status_tool(task_id: str) -> dict[str, Any]:
        return devclaw_status(task_id=task_id)

    @server.tool(
        name="devclaw_list",
        description=(
            "List recent devclaw tasks. Returns {tasks: [...]} with ts, task_id, kind, "
            "target_repo, status, duration_seconds, retries, pr_url per entry. "
            "Use for 'what's going on' or 'what did I ask you to do' queries. "
            "Optional: filter by status (done | failed | watchdog_killed)."
        ),
    )
    def _list_tool(limit: int = 20, status: str | None = None) -> dict[str, Any]:
        return devclaw_list(limit=limit, status=status)

    @server.tool(
        name="devclaw_logs",
        description=(
            "Get full context for a task: intent, acceptance criteria, result summary, "
            "blocker reason, PR url. Use this when a task is blocked or failed and you "
            "need context to understand what went wrong and help the user fix it."
        ),
    )
    def _logs_tool(task_id: str) -> dict[str, Any]:
        return devclaw_logs(task_id=task_id)

    @server.tool(
        name="devclaw_unblock",
        description=(
            "Unblock a blocked task by providing a decision. Pass the task_id and a "
            "decision string (the user's instruction for how to proceed). The orchestrator "
            "re-queues the task with the decision attached on the next sweep tick."
        ),
    )
    def _unblock_tool(task_id: str, decision: str) -> dict[str, Any]:
        return devclaw_unblock(task_id=task_id, decision=decision)

    return server


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="devclaw-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio for PC use; http for VPS container use)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for HTTP transport (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("DEVCLAW_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s devclaw-mcp %(message)s",
    )

    server = build_server()

    if args.transport == "http":
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run()  # stdio

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
