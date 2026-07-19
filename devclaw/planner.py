"""Program planner — turns a program goal into a task DAG via the GOAL DECOMPOSER.

ONE planning spine (ADR 0003, stage 1): the queue's program path used to run
its own coarse JSON planner (``plan_goal``, "aim for 1-6 tasks"); it now routes
through the same decomposer that plans durable goals
(:mod:`devclaw.goal.decomposer`), and this module is the thin adapter around
it: checklist items map ~1:1 onto :class:`PlannedTask` (id→key,
requirement+evidence_target→goal, depends_on→depends_on_keys, milestone,
scaffold). Cognition runs in Claude (we shell out to ``claude --print``); this
layer only maps + orders the validated structured output. Same split as the
runner: mechanism here, decisions in Claude. Auth comes from the bind-mounted
~/.claude session — no API key, ever.

Single goals (the "small bounded" case) still go through here: the decomposer
returns a one-item checklist with no deps. One code path; less special-casing.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from .state_store import TaskKind

if TYPE_CHECKING:
    from .goal.models import Checklist

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

@dataclass
class PlannedTask:
    #: stable model-assigned id used to express deps within this plan only
    key: str
    goal: str
    kind: TaskKind
    #: keys (not UUIDs) of other tasks in this plan that must finish first
    depends_on_keys: list[str] = field(default_factory=list)
    #: the milestone this task serves (decomposer-tagged; None when omitted)
    milestone: str | None = None
    #: True when the task is *generated scaffolding* (``ng new`` / ``dotnet
    #: new`` boilerplate), threaded verbatim from
    #: :attr:`devclaw.goal.models.ChecklistItem.scaffold` so the queue skips
    #: ONLY the adversarial review gate for it — the verify/build gate and the
    #: test-integrity scan still run (enforced in task_queue._run_and_settle).
    #: Without this thread a program-path scaffold diff would hit the review
    #: gate and fail closed on generator output.
    scaffold: bool = False


async def _plan_repo_context(workspace_dir: str) -> str:
    """Async wrapper — runs the blocking snapshot in a thread so it never blocks
    the event loop (same thread-offload rationale as task_queue's git wrappers).
    Looks up :func:`_review_repo_context_sync` as a module global so tests can
    patch it here."""
    return await asyncio.to_thread(_review_repo_context_sync, workspace_dir)


def order_tasks(tasks: list[PlannedTask]) -> list[PlannedTask]:
    """Validate the DAG shape and return tasks in topological order. Raises
    :class:`PlannerError` on duplicate keys, self-deps, dangling refs, or
    cycles. Kept separate from any parsing so every producer of
    ``list[PlannedTask]`` (today: the checklist adapter below; tests) goes
    through the SAME cycle check — ``validate_checklist`` prunes dangling and
    self deps but deliberately does not reject a multi-node cycle, and a cycle
    that reaches the queue deadlocks the DAG (no task ever becomes ready)."""
    seen: set[str] = set()
    for t in tasks:
        if t.key in seen:
            raise PlannerError(f"Duplicate task key '{t.key}'")
        seen.add(t.key)
    for t in tasks:
        for d in t.depends_on_keys:
            if d == t.key:
                raise PlannerError(f"Task '{t.key}' depends on itself")
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


#: Hard BRAKE on one program's task count — a cost backstop, NOT sizing
#: guidance (ADR 0003 §4 forbids numeric caps in planner prompts; §7 demands
#: mechanical spend brakes — this is the latter). The old plan_goal capped at
#: 20 against a prompt that aimed for 1-6; the decomposer is deliberately
#: finer-grained (real checklists run ~30 items), so the ceiling is generous —
#: it exists to stop a runaway decomposition (a whole-app goal exploding into
#: per-file micro-items) from enqueueing an unbounded fleet of sandboxed agent
#: runs, never to squeeze a legitimate plan.
MAX_PROGRAM_TASKS = 50


def planned_from_checklist(checklist: "Checklist") -> list[PlannedTask]:
    """Map a decomposer :class:`~devclaw.goal.models.Checklist` onto the
    queue's DAG shape — the near-1:1 adapter the unification rides on
    (ADR 0003 stage 1): id→key, requirement+evidence_target→goal,
    depends_on→depends_on_keys, milestone→milestone, scaffold→scaffold.

    The evidence target rides INSIDE the goal string (same carrier as the
    #252 acceptance-criteria brief) so both the worker and the pre-PR gate
    see the verifiable outcome the item was decomposed to produce. ``kind``
    is always ``implement_feature``: the decomposer emits work items, and
    the requirement text — not the kind enum — is what directs the agent."""
    if not checklist.items:
        # validate_checklist raises before this can happen today; belt+suspenders
        # so a future permissive parser can't hand the queue an empty DAG.
        raise PlannerError("decomposer produced no plannable items")
    if len(checklist.items) > MAX_PROGRAM_TASKS:
        raise PlannerError(
            f"decomposer produced {len(checklist.items)} tasks; the program "
            f"brake is {MAX_PROGRAM_TASKS}. Split the goal into smaller "
            "programs (or a durable goal, which executes its checklist "
            "incrementally) instead."
        )
    tasks: list[PlannedTask] = []
    for item in checklist.items:
        goal_text = item.requirement
        if item.evidence_target:
            goal_text += (
                "\n\nEvidence target (the verifiable outcome this task must "
                f"produce): {item.evidence_target}"
            )
        if item.note:
            goal_text += f"\nPlanner note: {item.note}"
        tasks.append(
            PlannedTask(
                key=item.id,
                goal=goal_text,
                kind="implement_feature",
                depends_on_keys=list(item.depends_on),
                milestone=item.milestone,
                scaffold=item.scaffold,
            )
        )
    return order_tasks(tasks)


async def plan_program(
    goal: str,
    workspace_dir: str,
    claude_caller: Optional[Callable[[str], Awaitable[str]]] = None,
) -> list[PlannedTask]:
    """Plan a program goal through the goal decomposer (the queue's
    ``_planner`` slot — ONE planning spine for programs and durable goals).

    The prompt is grounded in a snapshot of the ACTUAL workspace (remote,
    key-file presence, tracked layout — :func:`~devclaw.task_git._review_repo_context_sync`,
    the #227 pattern): without it the prompt was byte-invariant between a
    populated repo and an empty scaffold target, and host-side ``claude``
    inherits devclaw's own checkout as cwd — the wrong-codebase contamination
    channel #227 closed for the review gate. Strictly best-effort: a snapshot
    hiccup degrades to an ungrounded prompt, it never fails planning.

    Decomposer failures surface as :class:`PlannerError` so the queue's
    existing mark-program-failed + notify path handles them unchanged."""
    # Lazy imports: the decomposer's factory shells out to `claude` only when
    # called, and importing here (not at module top) keeps this module a leaf
    # for the many callers that only want PlannedTask/order_tasks.
    from .goal.decomposer import GoalDecomposerError, decompose, default_caller
    from .goal.models import Goal

    try:
        repo_context: str | None = await _plan_repo_context(workspace_dir)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail planning
        sys.stderr.write(
            f"devclaw: planner repo snapshot failed (planning ungrounded): {exc}\n"
        )
        repo_context = None
    # A throwaway in-memory Goal — the decomposer only reads its facts; nothing
    # is persisted (no GoalStore on this path; same shape as the CLI dry-run).
    goal_obj = Goal(
        id="program",
        objective=goal,
        cadence="",
        engine="devclaw",
        workspace_dir=workspace_dir,
    )
    try:
        checklist = await decompose(
            goal_obj,
            claude_caller=claude_caller or default_caller(),
            repo_context=repo_context,
        )
    except GoalDecomposerError as err:
        raise PlannerError(str(err), getattr(err, "raw", None)) from err
    return planned_from_checklist(checklist)
