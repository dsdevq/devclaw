"""research_task — LangGraph node that runs a `kind: research | draft | chore` spec.

Output: a `findings.md` (research/chore) or `draft.md` (draft) artifact next to the spec. No PR, no branch. The "deliverable" is a markdown file the operator reads.

Same subprocess-based shape as `code_task` — only the prompt and the expected output artifact differ.
"""

from __future__ import annotations

from datetime import timezone

from orchestrator.dispatch import now_utc
from orchestrator.runners._subprocess import run_claude
from orchestrator.state.models import GraphState, Result, TaskKind


def _artifact_name(kind: TaskKind) -> str:
    if kind == TaskKind.draft:
        return "draft.md"
    return "findings.md"  # research, chore


def _build_prompt(state: GraphState, output_dir_hint: str) -> str:
    spec = state.spec
    artifact = _artifact_name(spec.kind)
    return f"""You are a bounded autonomous research/draft/chore runner. Your task spec is below.
Read it, do the work, write the deliverable to `{artifact}` in the task directory, then emit a final JSON result line.

TASK SPEC:
  task_id: {spec.task_id}
  kind: {spec.kind.value}
  budget_seconds: {spec.budget.max_runtime_seconds}

VERBATIM INTENT:
{spec.verbatim_intent}

ACCEPTANCE CRITERIA:
{chr(10).join(f"  - {c}" for c in spec.acceptance_criteria)}

EXECUTION:
  1. The spec lives at a path under ~/.life/. The deliverable goes at the same directory level as the spec, file name `{artifact}`. If you can identify the spec path from context, write there. Otherwise write to `{output_dir_hint}/{artifact}` and report that path.
  2. Cite sources inline where relevant. Be specific. Don't hedge on conclusions that are clear.
  3. Print a JSON object to stdout on the LAST line of your output (and nothing after it) with this exact shape:
       {{"status": "done" | "blocked", "artifact_path": "<absolute path to {artifact}>", "notes": "<one-line summary>", "sources_cited": <int>, "blocker": "<if blocked>"}}

Stay inside the budget. If acceptance can't be met, stop and emit `status: blocked` with a blocker reason.
"""


def research_task_node(state: GraphState) -> dict:
    """LangGraph node — invoke Claude Code CLI for one research/draft/chore task."""
    spec = state.spec
    if spec.kind not in (TaskKind.research, TaskKind.draft, TaskKind.chore):
        return {
            "error": f"research_task_node received unexpected kind={spec.kind.value}",
        }

    # Output dir hint — falls back to /tmp if we can't infer from the spec
    output_dir_hint = f"/tmp/{spec.task_id}"

    sub = run_claude(
        _build_prompt(state, output_dir_hint),
        timeout_seconds=spec.budget.max_runtime_seconds,
    )

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
    artifact_path = data.get("artifact_path")
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc(),
            files_changed=[artifact_path] if artifact_path else [],
            notes=data.get("notes"),
        ),
    }


def research_task_node_stub(state: GraphState) -> dict:
    """Deterministic stub for unit testing."""
    spec = state.spec
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc().replace(tzinfo=timezone.utc),
            files_changed=[f"/tmp/{spec.task_id}/findings.md"],
            notes="stub research run — 4 sources cited",
        ),
    }
