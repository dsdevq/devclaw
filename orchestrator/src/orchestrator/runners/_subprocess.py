"""Generic Claude Code CLI subprocess helper.

All runners (`code_task`, `research_task`, `propose_change`) share the same shape: invoke `claude --print` with a prompt, capture stdout, parse a single-line JSON result on the last non-empty line. This module is the one place that handles the subprocess shape, timeout handling, stderr capture, and JSON parsing.

No API keys. Uses the user's Claude Code OAuth session at `~/.claude/`.

If/when we add a Codex CLI backend, this module gains an `agent_backend` parameter and a Codex-shaped variant. For v0.0.x, Claude only.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CLAUDE_BIN = "claude"

# Tools Claude Code is allowed to use inside the subprocess. Everything needed for an autonomous code change OR research read; no dangerous escape hatches.
DEFAULT_ALLOWED_TOOLS = "Bash,Edit,Read,Write,Glob,Grep,WebFetch"


@dataclass
class SubprocessResult:
    """What came back from one Claude CLI invocation."""

    status: str  # "done" | "blocked" — never trust this; parse from JSON line
    parsed_json: dict[str, Any] | None  # the last-line JSON, if any
    raw_stdout: str
    raw_stderr: str
    returncode: int
    timed_out: bool = False
    blocker: str | None = None  # set on failure paths

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and self.returncode == 0
            and self.parsed_json is not None
            and self.parsed_json.get("status") == "done"
        )


def _parse_last_json_line(stdout: str) -> dict[str, Any] | None:
    """Find the last non-empty line that's a JSON object. None if none found.

    Robust to leading whitespace and trailing blank lines. Tolerant of Claude's habit of emitting status / log lines before the final JSON.
    """
    for line in reversed([ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def run_claude(
    prompt: str,
    *,
    timeout_seconds: int,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = "acceptEdits",
) -> SubprocessResult:
    """Invoke `claude --print` with the given prompt. Return what happened.

    Does NOT raise — every failure path returns a SubprocessResult with `ok == False` and `blocker` set. Callers decide whether that's a retry, escalate, or terminal-blocked.
    """
    cmd = [
        CLAUDE_BIN,
        "--print",
        "--allowed-tools",
        allowed_tools,
        "--permission-mode",
        permission_mode,
        prompt,
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=partial or "",
            raw_stderr="",
            returncode=-1,
            timed_out=True,
            blocker="time_budget_exceeded",
        )
    except FileNotFoundError:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout="",
            raw_stderr=f"{CLAUDE_BIN}: command not found",
            returncode=-1,
            blocker="claude_cli_not_found",
        )

    if completed.returncode != 0:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout or "",
            raw_stderr=completed.stderr or "",
            returncode=completed.returncode,
            blocker=f"claude_cli_exit_{completed.returncode}",
        )

    parsed = _parse_last_json_line(completed.stdout)
    if parsed is None:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=0,
            blocker="no_parseable_result_json",
        )

    return SubprocessResult(
        status=parsed.get("status", "blocked"),
        parsed_json=parsed,
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        returncode=0,
        blocker=parsed.get("blocker") if parsed.get("status") == "blocked" else None,
    )
