"""propose_change — LangGraph node for drafting an RFC-style proposal markdown.

This is a `kind: draft`-shaped task that produces a proposal file in a project's `proposals/<date>-<slug>.md` path. The proposal is then human-reviewed and either approved (moved to `proposals-approved/`) or rejected.

Same subprocess shape as `research_task`. The difference is the prompt shape — proposal-drafting needs the structured RFC sections (Motivation / What changes / Step-by-step plan / Risks + mitigations / Acceptance criteria / Reply-in-chat-to-advance).

Routing: `task_intake` writes specs with `kind: draft` AND target the proposal flow. Both this runner and `research_task` accept `kind: draft`; we differentiate at the graph routing layer based on whether the spec carries a `project` field. (proposals are always project-bound; raw research drafts are not.)
"""

from __future__ import annotations

from datetime import timezone

from orchestrator.dispatch import now_utc
from orchestrator.runners._subprocess import run_claude
from orchestrator.state.models import GraphState, Result


PROPOSAL_TEMPLATE_HINT = """
---
status: new
project: {project}
drafted: {drafted_iso}
estimated_effort: <S | M | L — your estimate>
---

# <one-line title summarizing the change>

## Motivation

<Why this change. Reference plan.md / recon.md context if any. Stay grounded — no aspiration that the spec doesn't ask for.>

## What changes

<The concrete delta. Files, modules, behaviour. NOT how to implement — that's the next section.>

## Step-by-step plan

1. <First atomic-or-sequential step>
2. <Next step>
3. ...

## Impact on existing functionality

<What breaks. What stays. Migration concerns. Rollback story.>

## Risks + mitigations

- **Risk:** <one-liner>
  **Mitigation:** <one-liner>

## Acceptance criteria

1. <Testable / observable criterion>
2. ...

## Effort estimate

<S/M/L + brief justification>

## Reply in chat to advance

- `ship it` → moves to `proposals-approved/`
- `edit: <changes>` → Kit redrafts in place
- `reject` → moves to `proposals-rejected/`
"""


def _build_prompt(state: GraphState) -> str:
    spec = state.spec
    project = spec.project or "<unknown>"
    return f"""You are a bounded autonomous RFC drafter. Your task spec is below.
Read it, draft an RFC-style proposal markdown using the template, write it to a `proposals/<date>-<slug>.md` path under the project dir, then emit a final JSON result line.

TASK SPEC:
  task_id: {spec.task_id}
  kind: {spec.kind.value}
  project: {project}
  budget_seconds: {spec.budget.max_runtime_seconds}

VERBATIM INTENT:
{spec.verbatim_intent}

ACCEPTANCE CRITERIA (these gate the proposal itself, not the work the proposal proposes):
{chr(10).join(f"  - {c}" for c in spec.acceptance_criteria)}

EXECUTION:
  1. The project dir is ~/.life/projects/{project}/. The proposal goes at
     ~/.life/projects/{project}/proposals/<YYYY-MM-DD>-<short-slug>.md (lowercase, hyphenated slug).
  2. Use this template structure verbatim (frontmatter + 8 sections — fill, don't reorder):
     {PROPOSAL_TEMPLATE_HINT}
  3. Read the project's plan.md / recon.md if they exist, AND any prior proposals under proposals/ and proposals-approved/, so this RFC doesn't contradict approved direction.
  4. Print a JSON object to stdout on the LAST line of your output (and nothing after it):
       {{"status": "done" | "blocked", "proposal_path": "<absolute path>", "slug": "<the slug you chose>", "notes": "<one-line summary>", "blocker": "<if blocked>"}}

Stay inside the budget. RFC drafting should be ≤30 min wall-clock. If the spec is incoherent or asks for something already covered by an approved proposal, emit `status: blocked`.
"""


def propose_change_node(state: GraphState) -> dict:
    """LangGraph node — invoke Claude Code CLI for one proposal draft."""
    spec = state.spec
    if not spec.project:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="proposal_requires_project",
                notes="propose_change specs must carry a `project` field; task_intake should set it",
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
    proposal_path = data.get("proposal_path")
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc(),
            files_changed=[proposal_path] if proposal_path else [],
            notes=data.get("notes"),
        ),
    }


def propose_change_node_stub(state: GraphState) -> dict:
    """Deterministic stub for unit testing."""
    spec = state.spec
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc().replace(tzinfo=timezone.utc),
            files_changed=[f"~/.life/projects/{spec.project}/proposals/2026-05-18-stub.md"],
            notes="stub proposal draft",
        ),
    }
