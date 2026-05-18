"""code_task — LangGraph node that runs a `kind: code` spec via Claude Code CLI.

The agent loop happens INSIDE `claude --print`. From LangGraph's perspective this is one opaque cognition step.
"""

from __future__ import annotations

from datetime import timezone

from orchestrator.dispatch import now_utc
from orchestrator.runners._subprocess import run_claude
from orchestrator.state.models import GraphState, Result


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
  6. gh pr create --base {spec.target_branch} --title "<title>" --body "<body>"
  7. Print a JSON object to stdout on the LAST line of your output (and nothing after it) with this exact shape:
       {{"status": "done" | "blocked", "pr_url": "<url or null>", "branch": "kit/{spec.task_id}", "files_changed": [...], "tests_passed": true|false|null, "notes": "<one line>", "blocker": "<if blocked>"}}

Stay inside the budget. If acceptance can't be met, stop and emit `status: blocked` with a blocker reason.
"""


def code_task_node(state: GraphState) -> dict:
    """LangGraph node — invoke Claude Code CLI for one code task."""
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

    sub = run_claude(_build_prompt(state), timeout_seconds=spec.budget.max_runtime_seconds)

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
