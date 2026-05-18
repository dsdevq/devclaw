"""code_task — LangGraph node that runs a `kind: code` spec via Claude Code CLI subprocess.

The agent loop happens INSIDE `claude --print` — multi-turn, tool use, file editing, all internal to the subprocess. From LangGraph's perspective this is one opaque step.

No API key. Uses the user's Max subscription via the CLI's OAuth session at `~/.claude/`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timezone
from pathlib import Path

from orchestrator.dispatch import now_utc
from orchestrator.state.models import GraphState, Result, TaskStatus

CLAUDE_BIN = "claude"

# Tools Claude Code is allowed to use inside the subprocess. We grant everything needed for an autonomous code change but NOT the dangerous escape hatches.
DEFAULT_ALLOWED_TOOLS = "Bash,Edit,Read,Write,Glob,Grep,WebFetch"


def _build_prompt(state: GraphState) -> str:
    """Compose the prompt handed to the Claude Code subprocess.

    Deliberately terse — the spec carries the full intent. We're just telling Claude where to find it and what to do at the end.
    """
    spec = state.spec
    return f"""You are a bounded autonomous coding runner. Your task spec is below.
Read it, execute the work end-to-end, then write a `result.json` next to where the spec would live.

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
  6. gh pr create --base {spec.target_branch} --title "<title>" --body "<body>"
  7. Print a JSON object to stdout on the LAST line of your output (and nothing after it) with this exact shape:
       {{"status": "done" | "blocked", "pr_url": "<url or null>", "branch": "kit/{spec.task_id}", "files_changed": [...], "tests_passed": true|false|null, "notes": "<one line>", "blocker": "<if blocked>"}}

Stay inside the budget. If acceptance can't be met, stop and emit `status: blocked` with a blocker reason.
"""


def _parse_result(stdout: str, spec_task_id: str) -> Result:
    """Find the JSON line in Claude's output and parse it into a Result."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                return Result(
                    task_id=spec_task_id,
                    status=data.get("status", "blocked"),
                    completed_at=now_utc(),
                    pr_url=data.get("pr_url"),
                    branch=data.get("branch"),
                    files_changed=data.get("files_changed") or [],
                    tests_passed=data.get("tests_passed"),
                    notes=data.get("notes"),
                    blocker=data.get("blocker"),
                )
            except (json.JSONDecodeError, ValueError):
                continue

    # No parseable JSON found — treat as blocked with the tail of stdout as evidence.
    tail = "\n".join(lines[-5:])
    return Result(
        task_id=spec_task_id,
        status="blocked",
        completed_at=now_utc(),
        blocker="no_parseable_result_json",
        notes=f"Claude produced no parseable result-line. Tail:\n{tail[:500]}",
    )


def code_task_node(state: GraphState) -> dict:
    """LangGraph node — invokes Claude Code CLI as a subprocess for one code task.

    Returns a partial state update for LangGraph to merge.

    NOTE: This function does the actual subprocess call. In LangGraph 1.2+ this should be wrapped in `@task` if upstream nodes have side effects that shouldn't re-run on resume — but for this slice the node is the leaf cognition step, so a plain function is fine.
    """
    spec = state.spec
    if spec.kind.value != "code":
        return {
            "error": f"code_task_node received non-code spec (kind={spec.kind.value})",
        }
    if not spec.target_repo:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="target_repo_missing",
            ),
        }

    prompt = _build_prompt(state)
    cmd = [
        CLAUDE_BIN,
        "--print",
        "--allowed-tools",
        DEFAULT_ALLOWED_TOOLS,
        "--permission-mode",
        "acceptEdits",
        prompt,
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=spec.budget.max_runtime_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="time_budget_exceeded",
                notes=f"Claude Code subprocess exceeded {spec.budget.max_runtime_seconds}s. Tail:\n{partial[-500:]}",
                runtime_seconds=spec.budget.max_runtime_seconds,
            ),
        }

    if completed.returncode != 0:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker=f"claude_cli_exit_{completed.returncode}",
                notes=f"stderr tail: {(completed.stderr or '')[-500:]}",
            ),
        }

    return {"result": _parse_result(completed.stdout, spec.task_id)}


# Sanity stub used in tests when we don't want a real Claude invocation.
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
