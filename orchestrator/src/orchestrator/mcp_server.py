"""devclaw-mcp — a thin MCP server that bridges PC-side callers to the
VPS-side devclaw orchestrator.

Transport: stdio (the standard MCP local-tool transport). No new HTTP ports,
no new auth beyond what Tailscale SSH already provides.

Each tool call shells out via SSH to the VPS as the `lifekit` user (override
via env), invokes `devclaw-orchestrator <subcommand>`, captures stdout, and
returns the parsed JSON to the MCP caller. SSH/transport failures surface as
structured errors with an `error` field — the server stays alive across
failed calls.

Config:
  DEVCLAW_VPS_HOST  — VPS hostname (default: "lifekit-vps")
  DEVCLAW_VPS_USER  — SSH user            (default: "lifekit")
  DEVCLAW_SSH_BIN   — ssh binary to use   (default: "ssh", primarily for tests)

Source of truth for task state: the on-disk spec.yaml + result.json files
under `~/.life/tasks/<id>/` and `~/.life/projects/*/tasks/<id>/` on the VPS.
LangGraph's `orchestrator.sqlite` is a checkpointer, not a spec-level
register, so we read the observable artifacts. The VPS-side
`devclaw-orchestrator status` subcommand does this — we just shell out to it.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SSH_TIMEOUT_SECONDS = 60
INTAKE_TIMEOUT_SECONDS = 600


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
    """Build the argv to invoke `ssh user@host -- /bin/sh -c '<remote_cmd>'`."""
    return [
        cfg.ssh_bin,
        "-o", "BatchMode=yes",            # don't prompt for password
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg.user}@{cfg.host}",
        "--",
        "/bin/sh", "-c", remote_cmd,
    ]


def _run_ssh(
    cfg: SshConfig,
    remote_cmd: str,
    *,
    stdin_text: str | None = None,
    timeout: int = SSH_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run a remote command via SSH and return (rc, stdout, stderr).

    Raises FileNotFoundError if the ssh binary is missing — the caller wraps
    that as a structured tool error.
    """
    argv = _ssh_argv(cfg, remote_cmd)
    proc = subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _ssh_error(exc: Exception, *, action: str) -> dict[str, Any]:
    return {
        "error": f"ssh_failed_{action}",
        "detail": f"{type(exc).__name__}: {exc}",
    }


def devclaw_intake(prose: str, from_surface: str = "pc-kit") -> dict[str, Any]:
    """File a task against the VPS-side devclaw orchestrator.

    Parameter naming note: this tool takes `from_surface` (NOT `from`) because
    `from` is a Python reserved word and most MCP SDKs can't expose it cleanly
    as a kwarg. PC-Kit callers MUST pass `from_surface="pc-kit"`.

    Returns:
      Success: {task_id, spec_path, budget_min, target_repo, state}
        where `state` is `"new"` or `"duplicate"`.
      Failure: {error, detail}

    Idempotent: a byte-identical (prose, from_surface) pair returns the
    existing task_id with state="duplicate" without creating a second spec.
    """
    cfg = SshConfig.from_env()
    remote_cmd = (
        f"devclaw-orchestrator intake --from {shlex.quote(from_surface)}"
    )
    try:
        rc, stdout, stderr = _run_ssh(
            cfg, remote_cmd, stdin_text=prose, timeout=INTAKE_TIMEOUT_SECONDS
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_intake ssh failed: %s", exc)
        return _ssh_error(exc, action="intake")

    if rc != 0:
        return {
            "error": "intake_failed",
            "detail": stderr.strip() or stdout.strip() or f"rc={rc}",
        }
    # CLI emits a single-line JSON on stdout; any extra lines are progress that
    # leaked to stdout in test fakes. Grab the LAST non-empty line.
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {"error": "intake_no_output", "detail": stderr.strip()}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {
            "error": "intake_bad_json",
            "detail": f"{exc}: {lines[-1][:200]}",
        }


def devclaw_status(task_id: str) -> dict[str, Any]:
    """Look up the current state of a task by task_id.

    Reads the VPS-side on-disk spec.yaml + result.json — those are the
    observable source of truth (LangGraph's sqlite checkpointer holds graph
    state, not a stable spec-level register).

    Returns:
      Success: {state, last_action, pr_url?, completed_at?, ...}
        State is one of: "ready", "dispatched-*", "done", "blocked",
        "unknown" (no spec on disk for that id).
      Failure: {error, detail}
    """
    cfg = SshConfig.from_env()
    remote_cmd = f"devclaw-orchestrator status {shlex.quote(task_id)}"
    try:
        rc, stdout, stderr = _run_ssh(cfg, remote_cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("devclaw_status ssh failed: %s", exc)
        return _ssh_error(exc, action="status")

    if rc != 0:
        return {
            "error": "status_failed",
            "detail": stderr.strip() or stdout.strip() or f"rc={rc}",
        }
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {"error": "status_no_output", "detail": stderr.strip()}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {
            "error": "status_bad_json",
            "detail": f"{exc}: {lines[-1][:200]}",
        }


# ─── MCP server wiring ───────────────────────────────────────────────────────


def build_server():
    """Build the FastMCP server with both tools registered.

    Imported lazily so the `mcp` SDK is only required when actually starting
    the server. The tool *functions* above are pure and importable without
    the SDK — that's what tests exercise.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("devclaw")

    @server.tool(
        name="devclaw_intake",
        description=(
            "File a task against the VPS-side devclaw orchestrator. "
            "Parameter `from_surface` (NOT `from` — Python reserved word) is "
            "the provenance label, e.g. 'pc-kit'. Returns "
            "{task_id, spec_path, budget_min, target_repo, state} where state "
            "is 'new' or 'duplicate'. Idempotent on (prose, from_surface)."
        ),
    )
    def _intake_tool(prose: str, from_surface: str = "pc-kit") -> dict[str, Any]:
        return devclaw_intake(prose=prose, from_surface=from_surface)

    @server.tool(
        name="devclaw_status",
        description=(
            "Look up a task's current state. Reads ~/.life/tasks/<id>/ + "
            "~/.life/projects/*/tasks/<id>/ on the VPS (spec.yaml + "
            "result.json — the observable source of truth; the LangGraph "
            "sqlite checkpointer is not a spec-level register). Returns "
            "{state, last_action, pr_url?, completed_at?, ...}."
        ),
    )
    def _status_tool(task_id: str) -> dict[str, Any]:
        return devclaw_status(task_id=task_id)

    return server


def main() -> int:
    """Entry point for the `devclaw-mcp` console script."""
    logging.basicConfig(
        level=os.environ.get("DEVCLAW_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s devclaw-mcp %(message)s",
    )
    server = build_server()
    server.run()  # stdio transport by default in FastMCP
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
