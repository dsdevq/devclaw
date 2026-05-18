"""task_intake — convert a natural-language Telegram intent into a structured TaskSpec.

This is the ONLY runner whose output isn't a Result. It's a Pydantic TaskSpec, written to disk as `~/.life/tasks/<task_id>/spec.yaml` (atomic) or `~/.life/projects/<project>/tasks/<task_id>/spec.yaml` (project-bound) — the dispatch cron (or per-task graph) picks it up from there.

Same subprocess shape as the other runners — `claude --print` with a tightly-scoped prompt that asks Claude to emit a single JSON line that we validate as a TaskSpec.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from orchestrator.dispatch import now_utc, persist_spec
from orchestrator.runners._subprocess import run_claude
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)

logger = logging.getLogger(__name__)


def _short_hex(n: int = 4) -> str:
    return secrets.token_hex(n // 2)


def _build_intake_prompt(verbatim_intent: str) -> str:
    return f"""You are a task-spec generator. Convert the user's natural-language intent into a structured task spec JSON.

USER INTENT:
\"\"\"
{verbatim_intent}
\"\"\"

Decide:
  1. `kind` — one of: code (writes code, opens PR), research (synthesizes findings), draft (drafts a document), chore (mechanical bulk edit), decision (asks the user a yes/no/multi-choice). Default to `research` if unclear.
  2. `target_repo` — `<org>/<repo>` ONLY if kind is `code` AND the intent names a repo. Else null.
  3. `target_branch` — default "main" unless intent specifies.
  4. `project` — `<slug>` ONLY if intent clearly names an existing project (lifekit-stack, devclaw, finance-sentry, lifekit, swarm). Else null.
  5. `acceptance_criteria` — testable / observable criteria, max 5. For code: criteria the verifier can check via bash. For research/draft: criteria like "findings.md exists" or "covers section X". For chore: similar to code.
  6. `budget_seconds` — default 1800 (30 min) for code/research/draft/chore. 3600 (60 min) for non-trivial research. Cap 14400 (4h).

Print a JSON object to stdout on the LAST line of your output (and nothing after it):
  {{"kind": "...", "target_repo": "..." or null, "target_branch": "main", "project": "..." or null, "acceptance_criteria": [...], "budget_seconds": 1800, "notes": "<one-line clarification of what you decided and why>"}}

Be conservative — if the intent is ambiguous, prefer kind=research and add a clarifying note. Don't invent acceptance criteria the user didn't imply.
"""


def intake(
    verbatim_intent: str,
    *,
    requester_route: RequesterRoute,
    life_root: Path | None = None,
    task_id: str | None = None,
    created_by: str = "task_intake",
) -> TaskSpec | None:
    """Convert NL intent into a TaskSpec and write it under ~/.life/tasks/.

    Returns the TaskSpec on success, None on failure.

    Failure modes — return None, log:
      - Claude subprocess timed out / errored / produced no parseable JSON
      - Claude's output fails Pydantic validation as a partial-TaskSpec
    """
    life_root = life_root or Path("~/.life").expanduser()

    sub = run_claude(_build_intake_prompt(verbatim_intent), timeout_seconds=300)
    if sub.parsed_json is None:
        logger.warning("task_intake: claude returned no parseable JSON: %s", sub.blocker)
        return None

    data = sub.parsed_json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    derived_task_id = task_id or f"{today}-intake-{_short_hex(4)}"

    try:
        spec = TaskSpec(
            task_id=derived_task_id,
            created_at=now_utc(),
            created_by=created_by,
            requester_route=requester_route,
            verbatim_intent=verbatim_intent,
            kind=TaskKind(data.get("kind", "research")),
            acceptance_criteria=data.get("acceptance_criteria") or [],
            budget=Budget(max_runtime_seconds=int(data.get("budget_seconds", 1800))),
            target_repo=data.get("target_repo"),
            target_branch=data.get("target_branch", "main") or "main",
            project=data.get("project"),
            status=TaskStatus.ready,
        )
    except (ValidationError, ValueError) as exc:
        logger.warning("task_intake: claude output failed validation: %s", exc)
        return None

    # Pick the destination directory based on whether the spec is project-bound.
    if spec.project:
        task_dir = life_root / "projects" / spec.project / "tasks" / spec.task_id
    else:
        task_dir = life_root / "tasks" / spec.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    persist_spec(spec, task_dir / "spec.yaml")

    logger.info(
        "task_intake: spec written at %s (kind=%s, project=%s)",
        task_dir / "spec.yaml",
        spec.kind.value,
        spec.project,
    )
    return spec
