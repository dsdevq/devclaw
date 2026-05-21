"""Agent-CLI subprocess helpers — Claude Code OR OpenAI Codex CLI.

All runners (`code_task`, `research_task`, `propose_change`) share the same
shape: invoke an agent CLI with a prompt, capture stdout, parse a single-line
JSON result on the last non-empty line. This module is the one place that
handles the subprocess shape, timeout handling, stderr capture, and JSON
parsing.

Two backends live here side-by-side:

  - `run_claude()` — current default. Shells out to `claude --print` over
    the user's Claude Code OAuth session at `~/.claude/`.
  - `run_codex()`  — sibling adapter wrapping `codex exec --json`, over the
    user's ChatGPT Pro OAuth session at `~/.codex/auth.json`. Tails the
    Codex JSONL event stream (`thread.started` / `turn.*` / `item.completed`)
    and uses `--output-last-message` to capture the final assistant text.

Backend selection is data-driven via the `DEVCLAW_AGENT_BACKEND` env var
(or `cfg.backend` if a caller wires its own selector). Claude stays the
default until the 2026-06-15 Anthropic Agent SDK billing-split cutover, at
which point a follow-up PR will flip the default. See
`~/.life/system/adapters.md` and the migration proposal in
`~/.life/system/proposals.md` (anchor `2026-05-13-buildengine-migration-anthropic-to-codex`).

No API keys on either path. Both rely on a prior interactive `claude login`
/ `codex login` having written an OAuth token to `~/.claude/` / `~/.codex/`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ─── Claude backend ─────────────────────────────────────────────────────────

CLAUDE_BIN = "claude"

# Tools Claude Code is allowed to use inside the subprocess. Everything needed
# for an autonomous code change OR research read; no dangerous escape hatches.
DEFAULT_ALLOWED_TOOLS = "Bash,Edit,Read,Write,Glob,Grep,WebFetch"


# ─── Codex backend ──────────────────────────────────────────────────────────

CODEX_BIN = "codex"

# Pinned model — Codex's model lineup deprecates fast; treat the name as
# config not a constant. Override via `DEVCLAW_CODEX_MODEL` env var or by
# passing `model=` explicitly. `gpt-5.3-codex` is the autonomous-loop default
# per the 2026-05-17 BuildEngine migration research.
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"

# Codex sandbox scope. `workspace-write` is the right default for build tasks
# (lets the agent edit files + run shell inside its workdir but blocks writes
# outside it). `read-only` for research; `danger-full-access` only when an
# operator explicitly opts in.
DEFAULT_CODEX_SANDBOX: Literal["read-only", "workspace-write", "danger-full-access"] = (
    "workspace-write"
)


# ─── Backend selection ──────────────────────────────────────────────────────

AgentBackend = Literal["claude", "codex"]

# Default backend stays on claude until the 2026-06-15 Anthropic Agent SDK
# billing-split cutover (proposal:
# ~/.life/system/proposals.md#2026-05-13-buildengine-migration-anthropic-to-codex).
# Operators can opt into codex early by exporting DEVCLAW_AGENT_BACKEND=codex.
DEFAULT_AGENT_BACKEND: AgentBackend = "claude"


def select_agent_backend(default: AgentBackend = DEFAULT_AGENT_BACKEND) -> AgentBackend:
    """Read the configured agent backend.

    Resolution order:
      1. `DEVCLAW_AGENT_BACKEND` env var (`claude` | `codex`).
      2. `default` argument (caller-supplied override).
      3. The module-level `DEFAULT_AGENT_BACKEND` constant.

    Unknown values log a warning and fall back to the default.
    """
    raw = os.environ.get("DEVCLAW_AGENT_BACKEND")
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("claude", "codex"):
        return raw  # type: ignore[return-value]
    logger.warning("unknown DEVCLAW_AGENT_BACKEND=%r; falling back to %s", raw, default)
    return default


# ─── Shared result shape ────────────────────────────────────────────────────


@dataclass
class SubprocessResult:
    """What came back from one agent-CLI invocation.

    Both backends return this shape so callers don't branch on which CLI ran.
    `parsed_json` is the last-line JSON result the runner prompt asked for
    (see each runner's `_build_prompt`); `event_counts` is backend-specific
    telemetry (Codex JSONL events tallied) and is empty for Claude.
    """

    status: str  # "done" | "blocked" — never trust this; parse from JSON line
    parsed_json: dict[str, Any] | None  # the last-line JSON, if any
    raw_stdout: str
    raw_stderr: str
    returncode: int
    timed_out: bool = False
    blocker: str | None = None  # set on failure paths
    backend: AgentBackend = "claude"
    event_counts: dict[str, int] = field(default_factory=dict)

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

    Robust to leading whitespace and trailing blank lines. Tolerant of an
    agent's habit of emitting status / log lines before the final JSON.
    """
    for line in reversed([ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


# ─── Claude backend impl ────────────────────────────────────────────────────


def run_claude(
    prompt: str,
    *,
    timeout_seconds: int,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = "acceptEdits",
) -> SubprocessResult:
    """Invoke `claude --print` with the given prompt. Return what happened.

    Does NOT raise — every failure path returns a SubprocessResult with
    `ok == False` and `blocker` set. Callers decide whether that's a retry,
    escalate, or terminal-blocked.
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
            backend="claude",
        )
    except FileNotFoundError:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout="",
            raw_stderr=f"{CLAUDE_BIN}: command not found",
            returncode=-1,
            blocker="claude_cli_not_found",
            backend="claude",
        )

    if completed.returncode != 0:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout or "",
            raw_stderr=completed.stderr or "",
            returncode=completed.returncode,
            blocker=f"claude_cli_exit_{completed.returncode}",
            backend="claude",
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
            backend="claude",
        )

    return SubprocessResult(
        status=parsed.get("status", "blocked"),
        parsed_json=parsed,
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        returncode=0,
        blocker=parsed.get("blocker") if parsed.get("status") == "blocked" else None,
        backend="claude",
    )


# ─── Codex backend impl ─────────────────────────────────────────────────────


def _parse_codex_event_stream(jsonl: str) -> tuple[str | None, dict[str, int]]:
    """Walk a Codex `--json` JSONL stream and pull out (final_text, counts).

    Events recognised (per
    https://developers.openai.com/codex/noninteractive and Codex repo issue
    #14736):

      - `thread.started`  — emitted once, carries `thread_id`.
      - `turn.started` / `turn.completed` / `turn.failed`.
      - `item.started` / `item.updated` / `item.completed` — each with
        `item.item_type` ∈ {agent_message, reasoning, command_execution,
        file_change, mcp_tool_call, web_search, plan_update}.
      - `error`.

    `file_change` and `command_execution` items map to the existing
    BuildEngine "files-changed" + "shell-exec" surface; we count them so
    runners can surface telemetry. The last `agent_message` item's `text`
    field is treated as the final assistant message — this is where the
    runner's required last-line JSON result lives.

    Returns (`final_assistant_text`, event-type counts).
    """
    counts: dict[str, int] = {
        "thread.started": 0,
        "turn.started": 0,
        "turn.completed": 0,
        "turn.failed": 0,
        "item.started": 0,
        "item.completed": 0,
        "error": 0,
        "file_change": 0,
        "command_execution": 0,
        "agent_message": 0,
        "mcp_tool_call": 0,
    }
    final_text: str | None = None

    for raw in jsonl.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            # Codex occasionally interleaves non-JSON warnings; ignore them.
            continue
        etype = evt.get("type")
        if not isinstance(etype, str):
            continue
        counts[etype] = counts.get(etype, 0) + 1
        if etype == "item.completed":
            item = evt.get("item") or {}
            item_type = item.get("item_type") or item.get("type")
            if isinstance(item_type, str):
                counts[item_type] = counts.get(item_type, 0) + 1
                if item_type == "agent_message":
                    text = item.get("text") or item.get("message")
                    if isinstance(text, str) and text.strip():
                        final_text = text
    return final_text, counts


def run_codex(
    prompt: str,
    *,
    timeout_seconds: int,
    workdir: str | os.PathLike[str] | None = None,
    model: str | None = None,
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None,
    extra_args: list[str] | None = None,
) -> SubprocessResult:
    """Invoke `codex exec --json ...` with the given prompt. Return what happened.

    Mirrors `run_claude`'s contract: never raises; every failure path returns
    a `SubprocessResult` with `ok=False` and `blocker` set. Callers don't
    branch on backend — they consume `SubprocessResult` the same way.

    Model defaults to `DEFAULT_CODEX_MODEL` (pinned to `gpt-5.3-codex`) and
    can be overridden via the `model=` arg or `DEVCLAW_CODEX_MODEL` env var.
    Sandbox scope defaults to `workspace-write` and can be tightened to
    `read-only` for research-only tasks.

    Auth: relies on `codex login` having been run interactively once on the
    host; the OAuth token lives at `~/.codex/auth.json`. No API key
    required when ChatGPT Pro is active — the CLI uses the subscription
    pool by default.

    JSONL event stream is parsed in-process (see `_parse_codex_event_stream`).
    The final assistant message captured from `item.completed
    (agent_message)` is run through `_parse_last_json_line` to extract the
    runner-prompted result JSON.
    """
    resolved_model = (
        model
        or os.environ.get("DEVCLAW_CODEX_MODEL")
        or DEFAULT_CODEX_MODEL
    )
    resolved_sandbox = sandbox or DEFAULT_CODEX_SANDBOX

    cmd = [
        CODEX_BIN,
        "exec",
        "--json",
        "--sandbox",
        resolved_sandbox,
        "--model",
        resolved_model,
    ]
    if workdir is not None:
        cmd += ["--cd", str(workdir)]
    if extra_args:
        cmd += list(extra_args)
    cmd.append(prompt)

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
            backend="codex",
        )
    except FileNotFoundError:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout="",
            raw_stderr=f"{CODEX_BIN}: command not found",
            returncode=-1,
            blocker="codex_cli_not_found",
            backend="codex",
        )

    final_text, counts = _parse_codex_event_stream(completed.stdout)

    if completed.returncode != 0:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout or "",
            raw_stderr=completed.stderr or "",
            returncode=completed.returncode,
            blocker=f"codex_cli_exit_{completed.returncode}",
            backend="codex",
            event_counts=counts,
        )

    if final_text is None:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=0,
            blocker="codex_no_final_agent_message",
            backend="codex",
            event_counts=counts,
        )

    parsed = _parse_last_json_line(final_text)
    if parsed is None:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=0,
            blocker="no_parseable_result_json",
            backend="codex",
            event_counts=counts,
        )

    return SubprocessResult(
        status=parsed.get("status", "blocked"),
        parsed_json=parsed,
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        returncode=0,
        blocker=parsed.get("blocker") if parsed.get("status") == "blocked" else None,
        backend="codex",
        event_counts=counts,
    )


# ─── Unified entrypoint ─────────────────────────────────────────────────────


def run_agent(
    prompt: str,
    *,
    timeout_seconds: int,
    backend: AgentBackend | None = None,
    workdir: str | os.PathLike[str] | None = None,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = "acceptEdits",
    codex_model: str | None = None,
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None,
) -> SubprocessResult:
    """Dispatch a single agent-CLI invocation to the configured backend.

    `backend=None` reads `select_agent_backend()` (env-var-driven). Pass
    `backend=` explicitly when a caller has already resolved it (e.g.
    `code_task` reads it once per run and then forks for the in-sandbox
    helper).
    """
    resolved = backend or select_agent_backend()
    if resolved == "codex":
        return run_codex(
            prompt,
            timeout_seconds=timeout_seconds,
            workdir=workdir,
            model=codex_model,
            sandbox=codex_sandbox,
        )
    return run_claude(
        prompt,
        timeout_seconds=timeout_seconds,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
    )


__all__ = [
    "AgentBackend",
    "CLAUDE_BIN",
    "CODEX_BIN",
    "DEFAULT_AGENT_BACKEND",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_CODEX_MODEL",
    "DEFAULT_CODEX_SANDBOX",
    "SubprocessResult",
    "_parse_codex_event_stream",
    "_parse_last_json_line",
    "run_agent",
    "run_claude",
    "run_codex",
    "select_agent_backend",
]
