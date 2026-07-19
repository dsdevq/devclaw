"""Planner — turns a single high-level goal into a DAG of OpenHands tasks.

Cognition runs in Claude (we shell out to ``claude --print``); this layer only
validates the JSON the model produces. Same split as the runner: mechanism
here, decisions in Claude. Auth comes from the bind-mounted ~/.claude session —
no API key, ever.

Single goals (the "small bounded" case) still go through here: the planner
returns a one-element list with no deps. One code path; less special-casing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .state_store import TaskKind

# The low-level LLM-call primitive lives in llm_call.py — a LEAF module
# (extracted 2026-07-19) so the quality gate imports the call primitive
# without dragging this module's state_store/task_git deps behind it.
# Re-exported verbatim: every historical `from .planner import …` call site —
# and tests patching `planner.call_claude` (cognition.py resolves it lazily
# through THIS namespace) — keeps working unchanged.
from .llm_call import (  # noqa: F401
    _COGNITION_TIMEOUT_DEFAULT_S,
    _build_claude_argv,
    _cognition_timeout_ms_from_env,
    CLAUDE_BIN,
    CliEnvelope,
    PLANNER_TIMEOUT_MS,
    PlannerError,
    call_claude,
    claude_with_model,
    extract_json,
    parse_cli_envelope,
)

# Imported as a module global (not called via task_git.) so tests can patch it
# on THIS namespace — same convention as task_queue's git wrappers.
from .task_git import _review_repo_context_sync  # noqa: F401

MAX_TASKS_PER_PLAN = 20

# Per-role model tiering. Running every cognition call on the account default
# (Opus) burns the Pro/Max quota fast and is slow; tier each role to the lightest
# model that does its job. These are `claude --model` values (an alias like
# 'sonnet'/'opus', or a full id). Planning is rare + high-leverage → Opus; the
# scope grill is conversational → Sonnet (set in elicitation.py); the eval judge
# is bounded classification → Haiku (set in eval_judge.py). The heavy coding path
# (OpenHands) is tiered separately via DEVCLAW_EXEC_MODEL. An empty value →
# the CLI's account default (no --model flag passed).
from .model_tiers import model_for as _model_for
PLANNER_MODEL = _model_for("planner")

VALID_KINDS: tuple[TaskKind, ...] = ("implement_feature", "fix_bug", "review_repository")


@dataclass
class PlannedTask:
    #: stable model-assigned id used to express deps within this plan only
    key: str
    goal: str
    kind: TaskKind
    #: keys (not UUIDs) of other tasks in this plan that must finish first
    depends_on_keys: list[str] = field(default_factory=list)
    #: the spec milestone this task serves (plan-from-spec only; else None)
    milestone: str | None = None


async def _plan_repo_context(workspace_dir: str) -> str:
    """Async wrapper — runs the blocking snapshot in a thread so it never blocks
    the event loop (same thread-offload rationale as task_queue's git wrappers).
    Looks up :func:`_review_repo_context_sync` as a module global so tests can
    patch it here."""
    return await asyncio.to_thread(_review_repo_context_sync, workspace_dir)


def build_planner_prompt(
    goal: str, workspace_dir: str, repo_context: str | None = None
) -> str:
    from .prompts import load_prompt

    parts = [load_prompt("plan-goal")]
    if repo_context and repo_context.strip():
        parts.append(
            "REPOSITORY CONTEXT (facts from the task workspace — the source of "
            "truth for repo identity and which files/dirs exist):\n"
            + repo_context.strip()
        )
    parts.append(
        f"Workspace: {workspace_dir}\n"
        f"Goal: {goal}\n\n"
        "Return the JSON now."
    )
    return "\n\n".join(parts)


def validate_plan(parsed: object) -> list[PlannedTask]:
    """Validate the parsed plan and return tasks in topological order. Raises
    PlannerError on cycles, dangling refs, missing fields, etc."""
    if not isinstance(parsed, dict):
        raise PlannerError("Plan must be a JSON object")
    raw = parsed.get("tasks")
    if not isinstance(raw, list):
        raise PlannerError("Plan.tasks must be an array")
    if len(raw) == 0:
        raise PlannerError("Plan must contain at least one task")
    if len(raw) > MAX_TASKS_PER_PLAN:
        raise PlannerError(
            f"Plan has {len(raw)} tasks; max is {MAX_TASKS_PER_PLAN}. Refine the goal."
        )

    seen: set[str] = set()
    tasks: list[PlannedTask] = []
    for t in raw:
        if not isinstance(t, dict):
            raise PlannerError("Each task must be an object")
        key = t.get("key").strip() if isinstance(t.get("key"), str) else ""
        goal = t.get("goal").strip() if isinstance(t.get("goal"), str) else ""
        kind_raw = t.get("kind") if isinstance(t.get("kind"), str) else "implement_feature"
        deps_raw = t.get("depends_on")
        milestone = t.get("milestone").strip() if isinstance(t.get("milestone"), str) else None
        if not key:
            raise PlannerError("Task missing 'key'")
        if not goal:
            raise PlannerError(f"Task '{key}' missing 'goal'")
        if key in seen:
            raise PlannerError(f"Duplicate task key '{key}'")
        if kind_raw not in VALID_KINDS:
            raise PlannerError(
                f"Task '{key}' has invalid kind '{kind_raw}'; "
                f"expected one of {', '.join(VALID_KINDS)}"
            )
        depends_on_keys: list[str] = []
        if deps_raw is not None:
            if not isinstance(deps_raw, list):
                raise PlannerError(f"Task '{key}' depends_on must be an array")
            for d in deps_raw:
                if not isinstance(d, str) or not d.strip():
                    raise PlannerError(f"Task '{key}' has non-string dep")
                if d == key:
                    raise PlannerError(f"Task '{key}' depends on itself")
                depends_on_keys.append(d.strip())
        seen.add(key)
        tasks.append(
            PlannedTask(
                key=key,
                goal=goal,
                kind=kind_raw,
                depends_on_keys=depends_on_keys,
                milestone=milestone or None,
            )
        )

    # Validate all dep refs resolve.
    for t in tasks:
        for d in t.depends_on_keys:
            if d not in seen:
                raise PlannerError(f"Task '{t.key}' depends on unknown key '{d}'")

    # Kahn topological sort — also detects cycles.
    by_key = {t.key: t for t in tasks}
    indegree = {t.key: len(t.depends_on_keys) for t in tasks}
    dependents: dict[str, list[str]] = {}
    for t in tasks:
        for d in t.depends_on_keys:
            dependents.setdefault(d, []).append(t.key)

    ready = sorted(k for k, n in indegree.items() if n == 0)
    ordered: list[PlannedTask] = []
    while ready:
        k = ready.pop(0)
        ordered.append(by_key[k])
        for d in dependents.get(k, []):
            indegree[d] -= 1
            if indegree[d] == 0:
                ready.append(d)
        ready.sort()  # deterministic order across runs

    if len(ordered) != len(tasks):
        raise PlannerError("Plan contains a dependency cycle")
    return ordered


#: planning (plan_goal + plan_spec) runs at the planner tier
_planner_caller = claude_with_model(PLANNER_MODEL, role="planner")


def _parse_plan(raw: str) -> list[PlannedTask]:
    """Extract → parse → validate a planner response into an ordered DAG."""
    json_text = extract_json(raw)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise PlannerError(f"Planner JSON parse failed: {err}", raw) from err
    return validate_plan(parsed)


async def plan_goal(
    goal: str,
    workspace_dir: str,
    claude_caller: Callable[[str], Awaitable[str]] = _planner_caller,
) -> list[PlannedTask]:
    """Plan a bare goal string (the small-bounded `start_program` case).

    The prompt is grounded in a snapshot of the ACTUAL workspace (remote,
    key-file presence, tracked layout — :func:`~devclaw.task_git._review_repo_context_sync`,
    the #227 pattern): without it the prompt was byte-invariant between a
    populated repo and an empty scaffold target, and host-side ``claude``
    inherits devclaw's own checkout as cwd — the wrong-codebase contamination
    channel #227 closed for the review gate. Strictly best-effort: a snapshot
    hiccup degrades to an ungrounded prompt, it never fails planning."""
    try:
        repo_context: str | None = await _plan_repo_context(workspace_dir)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail planning
        sys.stderr.write(
            f"devclaw: planner repo snapshot failed (planning ungrounded): {exc}\n"
        )
        repo_context = None
    raw = await claude_caller(build_planner_prompt(goal, workspace_dir, repo_context))
    return _parse_plan(raw)


# ===== plan-from-spec ========================================================
# The build-a-project-from-scratch path: decompose an *approved spec* (the
# shared scope contract handed in by the OpenClaw waiter after scope_grill) into
# a milestone-ordered DAG. Richer than plan_goal — the model is grounded in the
# spec's milestones, acceptance criteria, scope, and constraints.

def build_spec_planner_prompt(spec: str, workspace_dir: str) -> str:
    from .prompts import load_prompt

    return (
        f"{load_prompt('plan-spec')}\n\n"
        f"Workspace: {workspace_dir}\n\n"
        f"APPROVED SPEC:\n{spec}\n\n"
        "Return the JSON now."
    )


async def plan_spec(
    spec: str,
    workspace_dir: str,
    claude_caller: Callable[[str], Awaitable[str]] = _planner_caller,
) -> list[PlannedTask]:
    """Decompose an approved spec into a milestone-ordered DAG. Same validated
    DAG shape as plan_goal, with per-task milestones populated."""
    raw = await claude_caller(build_spec_planner_prompt(spec, workspace_dir))
    return _parse_plan(raw)
