"""code_task — LangGraph node that runs a `kind: code` spec via Claude Code CLI.

The agent loop happens INSIDE `claude --print`. From LangGraph's perspective this is one opaque cognition step.

As of 2026-05-20 this runner spawns the `claude` invocation through the **Sandbox port** (`orchestrator.sandbox`). The port picks the active adapter from `config/sandbox.yaml`; `sandcastle` (ephemeral per-task Docker container) is the production default, with `in_process` kept as a safety-net fallback. This closes the "No per-task sandbox isolation" gap and the /tmp disk-bloat trend captured in journal/2026-05-20.md.
"""

from __future__ import annotations

import json
import logging
from datetime import timezone
from typing import Any

from orchestrator.dispatch import now_utc
from orchestrator.runners._subprocess import (
    DEFAULT_ALLOWED_TOOLS,
    SubprocessResult,
    _parse_last_json_line,
)
from orchestrator.sandbox import Sandbox, load_sandbox, load_sandbox_config
from orchestrator.state.models import GraphState, Result

logger = logging.getLogger(__name__)

CLAUDE_BIN = "claude"


def _build_prompt(state: GraphState) -> str:
    spec = state.spec
    return f"""You are a bounded autonomous coding runner. Your task spec is below.
Read it, execute the work end-to-end, then emit a final JSON result line.

TASK SPEC:
  task_id: {spec.task_id}
  kind: {spec.kind.value}
  target_repo: {spec.target_repo}
  target_branch: {spec.target_branch}
  budget_seconds: {spec.budget.max_runtime_seconds}
  branch_name: kit/{spec.task_id}

VERBATIM INTENT:
{spec.verbatim_intent}

ACCEPTANCE CRITERIA:
{chr(10).join(f"  - {c}" for c in spec.acceptance_criteria)}

EXECUTION:
  1. mkdir /tmp/{spec.task_id} && cd /tmp/{spec.task_id}
  2. git clone https://github.com/{spec.target_repo}.git .
  3. git checkout -b kit/{spec.task_id}
  4. Implement against the acceptance criteria.
  5. git add -A && git commit -m "<concise present-tense title>" && git push -u origin kit/{spec.task_id}
  6. Idempotently ensure the `devclaw` label exists on {spec.target_repo} (safe to re-run):
       gh label create devclaw --repo {spec.target_repo} --color 1f6feb \\
         --description "Opened by the devclaw autonomous orchestrator. Branch pattern: kit/<task_id>-*. See ~/.life/projects/<project>/tasks/<id>/spec.yaml for the spec." \\
         --force 2>/dev/null || true
  7. gh pr create --base {spec.target_branch} --label devclaw --title "<title>" --body "<body>"
  8. Print a JSON object to stdout on the LAST line of your output (and nothing after it) with this exact shape:
       {{"status": "done" | "blocked", "pr_url": "<url or null>", "branch": "kit/{spec.task_id}", "files_changed": [...], "tests_passed": true|false|null, "notes": "<one line>", "blocker": "<if blocked>"}}

Stay inside the budget. If acceptance can't be met, stop and emit `status: blocked` with a blocker reason.
"""


def _build_claude_command(prompt: str) -> list[str]:
    """Construct the `claude --print` argv. Same shape `_subprocess.run_claude` uses, but as a list of arguments so the Sandbox adapter can feed it to a container.

    Kept here (rather than imported from `_subprocess`) because the sandbox boundary owns the argv — `_subprocess` is the host-side path and shouldn't grow a coupling to sandbox-shaped callers.
    """
    return [
        CLAUDE_BIN,
        "--print",
        "--allowed-tools",
        DEFAULT_ALLOWED_TOOLS,
        "--permission-mode",
        "acceptEdits",
        prompt,
    ]


def _readonly_mounts_from_config(config: dict[str, Any]) -> dict[str, str]:
    mounts = config.get("readonly_mounts") or {}
    if not isinstance(mounts, dict):
        return {}
    return {str(k): str(v) for k, v in mounts.items()}


def _sub_result_from_sandbox(sandbox_result, *, command_label: str = CLAUDE_BIN) -> SubprocessResult:
    """Translate SandboxResult → SubprocessResult so downstream code is unchanged."""
    if sandbox_result.timed_out:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=sandbox_result.stdout,
            raw_stderr=sandbox_result.stderr,
            returncode=-1,
            timed_out=True,
            blocker="time_budget_exceeded",
        )
    if sandbox_result.returncode != 0:
        # Sandbox-specific signal: e.g. sandcastle/docker missing OR claude exited non-zero. The stderr tail will tell the operator which.
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=sandbox_result.stdout,
            raw_stderr=sandbox_result.stderr,
            returncode=sandbox_result.returncode,
            blocker=f"sandbox_exit_{sandbox_result.returncode}",
        )

    parsed = _parse_last_json_line(sandbox_result.stdout)
    if parsed is None:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=sandbox_result.stdout,
            raw_stderr=sandbox_result.stderr,
            returncode=0,
            blocker="no_parseable_result_json",
        )
    return SubprocessResult(
        status=parsed.get("status", "blocked"),
        parsed_json=parsed,
        raw_stdout=sandbox_result.stdout,
        raw_stderr=sandbox_result.stderr,
        returncode=0,
        blocker=parsed.get("blocker") if parsed.get("status") == "blocked" else None,
    )


def _run_via_sandbox(
    state: GraphState,
    *,
    sandbox: Sandbox | None = None,
) -> SubprocessResult:
    """Build the agent command and dispatch it through the Sandbox port."""
    spec = state.spec
    cfg = load_sandbox_config()
    sb = sandbox if sandbox is not None else load_sandbox()
    prompt = _build_prompt(state)
    agent_cmd = _build_claude_command(prompt)
    sandbox_result = sb.run(
        task_id=spec.task_id,
        repo_url=f"https://github.com/{spec.target_repo}.git" if spec.target_repo else "",
        branch_strategy="new-branch",
        agent_command=agent_cmd,
        env={},
        readonly_mounts=_readonly_mounts_from_config(cfg),
        timeout_seconds=spec.budget.max_runtime_seconds,
    )
    logger.info(
        "code_task.sandbox_done task_id=%s runtime=%s rc=%s container=%s notes=%s",
        spec.task_id,
        sandbox_result.runtime,
        sandbox_result.returncode,
        sandbox_result.container_name,
        json.dumps(sandbox_result.notes),
    )
    return _sub_result_from_sandbox(sandbox_result)


def code_task_node(state: GraphState, *, sandbox: Sandbox | None = None) -> dict:
    """LangGraph node — invoke Claude Code CLI for one code task, via the Sandbox port.

    `sandbox` override is for tests; production callers leave it None and the port reads `config/sandbox.yaml`.
    """
    spec = state.spec
    if spec.kind.value != "code":
        return {"error": f"code_task_node received non-code spec (kind={spec.kind.value})"}
    if not spec.target_repo:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="target_repo_missing",
            ),
        }

    sub = _run_via_sandbox(state, sandbox=sandbox)

    if not sub.ok:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker=sub.blocker or "subprocess_failed",
                notes=f"stderr tail: {sub.raw_stderr[-500:]}" if sub.raw_stderr else None,
                runtime_seconds=spec.budget.max_runtime_seconds if sub.timed_out else None,
            ),
        }

    data = sub.parsed_json or {}
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc(),
            pr_url=data.get("pr_url"),
            branch=data.get("branch"),
            files_changed=data.get("files_changed") or [],
            tests_passed=data.get("tests_passed"),
            notes=data.get("notes"),
        ),
    }


def code_task_node_stub(state: GraphState) -> dict:
    """Deterministic stub for unit testing the graph wiring without burning tokens."""
    spec = state.spec
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc().replace(tzinfo=timezone.utc),
            pr_url=f"https://example.test/{spec.target_repo}/pull/1",
            branch=f"kit/{spec.task_id}",
            files_changed=["stub.md"],
            tests_passed=True,
            notes="stub run — no real Claude invocation",
        ),
    }
