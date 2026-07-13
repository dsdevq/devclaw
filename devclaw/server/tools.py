"""All MCP tool decorators — the chef's menu.

Each tool delegates to the long-lived services in ``_state`` (queue, store,
goals, registry) or to a sibling module (``deploy``, ``repo``). The tools stay
thin on purpose: validate inputs, dispatch, return JSON. Cognition lives below
(planner / evaluator / review gate), not here.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Optional

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..delivery import deploy as _deploy
from .. import elicitation as _elicitation
from ..delivery import repo as _repo
from ..project_registry import ProjectExists, project_rollup
from ._state import _goal_get, goals, mcp, queue, registry, store


@mcp.tool
async def dispatch_task(
    kind: Literal["implement_feature", "fix_bug", "review_repository"],
    workspace_dir: str,
    goal: str,
    notify_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = False,
) -> str:
    """One-shot dispatch of a code task to OpenHands in the given workspace_dir.
    Returns a task_id immediately; the task runs asynchronously. Poll
    get_status(task_id), or pass notify_url to be pushed the result.

    ``kind`` selects the prompt bias:
      - ``implement_feature`` — new features / open-ended changes.
      - ``fix_bug`` — biases toward reading existing code first, making the
        smallest fix, not refactoring unrelated code, and running the tests.
      - ``review_repository`` — READ-ONLY code review; the agent inspects the
        workspace and writes a report, prompt-instructed NOT to modify any
        files. ``verify_cmd`` and ``open_pr`` are ignored for this kind.

    Pass verify_cmd (e.g. "dotnet test", "npm run build && npm run test:ci") to
    gate the task: after the agent finishes, DevClaw runs that command in the
    workspace and the task only succeeds if it exits 0 — the agent's own
    "I'm done" is not trusted. A failing gate marks the task failed with the
    command output captured.

    Pass open_pr=True to DELIVER a successful change as something you review: on
    ``done``, DevClaw commits it to a branch, pushes, and opens a PR (best-effort;
    needs git push auth + a GitHub remote), recording the PR URL on the task.

    Prefer this over the older ``implement_feature`` / ``fix_bug`` /
    ``review_repository`` tools — those are kept as back-compat aliases and
    forward here."""
    if not workspace_dir or not goal:
        raise ToolError("dispatch_task requires workspace_dir and goal")
    is_review = kind == "review_repository"
    task_id = queue.submit(
        kind=kind,
        workspace_dir=workspace_dir,
        goal=goal,
        notify_url=notify_url,
        verify_cmd=None if is_review else verify_cmd,
        deliver=False if is_review else open_pr,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


@mcp.tool
async def implement_feature(
    workspace_dir: str,
    goal: str,
    notify_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = False,
) -> str:
    """DEPRECATED — thin forwarder to ``dispatch_task(kind="implement_feature")``.
    Kept for back-compat with existing MCP callers; prefer ``dispatch_task``
    for new integrations. See ``dispatch_task`` for full docs."""
    return await dispatch_task(
        kind="implement_feature",
        workspace_dir=workspace_dir,
        goal=goal,
        notify_url=notify_url,
        verify_cmd=verify_cmd,
        open_pr=open_pr,
    )


@mcp.tool
async def fix_bug(
    workspace_dir: str,
    description: str,
    notify_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = False,
) -> str:
    """DEPRECATED — thin forwarder to ``dispatch_task(kind="fix_bug")``.
    Kept for back-compat with existing MCP callers; prefer ``dispatch_task``
    for new integrations. See ``dispatch_task`` for full docs."""
    if not description:
        raise ToolError("fix_bug requires workspace_dir and description")
    return await dispatch_task(
        kind="fix_bug",
        workspace_dir=workspace_dir,
        goal=description,
        notify_url=notify_url,
        verify_cmd=verify_cmd,
        open_pr=open_pr,
    )


@mcp.tool
async def review_repository(
    workspace_dir: str, focus: str = "", notify_url: Optional[str] = None
) -> str:
    """DEPRECATED — thin forwarder to ``dispatch_task(kind="review_repository")``.
    Kept for back-compat with existing MCP callers; prefer ``dispatch_task``
    for new integrations. See ``dispatch_task`` for full docs."""
    return await dispatch_task(
        kind="review_repository",
        workspace_dir=workspace_dir,
        goal=focus or "general code review",
        notify_url=notify_url,
    )


@mcp.tool
async def review_trends(scope: str = "harness_self", limit_chars: int = 5000) -> str:
    """Read recent trend observations produced by devclaw's cross-session trend
    detector. Returns the tail of the matching ``trends.md`` as JSON
    ``{scope, path, trends}``.

    Pass ``scope='harness_self'`` (default) for devclaw's own self-observability
    file (in Denys's vault by default). Pass a workspace path for that project's
    per-repo trends (``<workspace>/.devclaw/trends.md``). The detector observes
    and surfaces patterns (recurring fixes, AGENTS.md drift, steering frequency,
    etc.); humans decide which to promote into AGENTS.md."""
    return json.dumps(goals.read_trends(scope=scope, limit_chars=limit_chars), indent=2)



@mcp.tool
async def onboard(
    workspace_dir: str, focus: str = "", notify_url: Optional[str] = None
) -> str:
    """Onboard a repository: analyze it and write a DRAFT documentation set so
    future tasks + humans start informed. OpenHands inspects the workspace
    READ-ONLY (it modifies no file except the four docs it writes) and captures
    COMPREHENSION ONLY across four scoped documents:

      - AGENTS.md      — agent-facing: stack, layout, build/run/test commands
                         (with the verify gate), conventions, gotchas.
      - README.md      — human-facing: one-paragraph purpose, quickstart,
                         high-level pointer at layout, one-line status.
      - ARCHITECTURE.md — component map, data flow, cross-cutting concerns,
                         notable design decisions (cross-links to DECISIONS).
      - DECISIONS.md   — ADR-style entries reconstructed from git log + code +
                         prior docs. Empty-with-header is acceptable when
                         nothing is honestly inferrable.

    Closes the C6 gap (`plan.md` §Production-ready): a project with only
    AGENTS.md is undocumented from a human's point of view. The onboarding
    skill (`skills/onboard/00-onboard.md`) enforces boundary discipline
    (no ADR reasoning in README, no quickstart in ARCHITECTURE) so the four
    docs don't blur into each other.

    Human-in-the-loop: each doc lands with a top-of-file DRAFT marker and is
    NOT authoritative until you review it. The agent won't clobber a
    substantive existing doc — it validates each part against the real repo
    and only corrects what's wrong or missing. Returns task_id immediately;
    same optional notify_url as implement_feature."""
    if not workspace_dir:
        raise ToolError("onboard requires workspace_dir")
    task_id = queue.submit(
        kind="onboard",
        workspace_dir=workspace_dir,
        goal=focus or "general onboarding",
        notify_url=notify_url,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


@mcp.tool
async def start_program(
    workspace_dir: str, goal: str, notify_url: Optional[str] = None
) -> str:
    """Submit a high-level coding goal for DevClaw to decompose into a DAG of
    smaller OpenHands tasks. The planner (a Claude subprocess) writes the plan,
    then tasks execute in dep order with bounded parallelism. Returns a
    program_id immediately; poll get_program(program_id) or pass notify_url to
    be pushed the final result when the whole program terminates. Use for goals
    too large for one implement_feature call."""
    if not workspace_dir or not goal:
        raise ToolError("start_program requires workspace_dir and goal")
    program_id = queue.submit_program(
        workspace_dir=workspace_dir, goal=goal, notify_url=notify_url
    )
    return json.dumps({"program_id": program_id, "status": "planning"}, indent=2)


@mcp.tool
async def get_status(task_id: str) -> str:
    """Return the current status + (when terminated) the result or error of a
    task. Status values: pending | running | done | failed | cancelled."""
    task = store.get_task(task_id)
    if not task:
        raise ToolError(f"unknown task_id: {task_id}")
    return json.dumps(task.to_dict(), indent=2)


@mcp.tool
async def get_program(program_id: str) -> str:
    """Return a program row and all its tasks in dependency order. Use to poll a
    program submitted via start_program."""
    program = store.get_program(program_id)
    if not program:
        raise ToolError(f"unknown program_id: {program_id}")
    tasks = store.list_program_tasks(program_id)
    return json.dumps(
        {"program": program.to_dict(), "tasks": [t.to_dict() for t in tasks]}, indent=2
    )


@mcp.tool
async def list_programs(limit: Annotated[int, Field(ge=1, le=1000)] = 50) -> str:
    """List recent programs (goals submitted via start_program), most-recent
    first. Use to discover program_ids for get_program, get_events, or
    /dashboard."""
    programs = store.list_programs(limit=limit)
    return json.dumps([p.to_dict() for p in programs], indent=2)


@mcp.tool
async def get_events(
    program_id: Optional[str] = None,
    task_id: Optional[str] = None,
    since_id: Optional[int] = None,
    limit: Annotated[int, Field(ge=1, le=5000)] = 500,
) -> str:
    """Return events emitted by the OpenHands runner for one program or one
    task, in emission order. Each event has an id (monotonic cursor), type,
    source, payload_json (the raw SDK Event), and ts. Pass since_id to resume —
    same semantics as the /programs/:id/events SSE Last-Event-Id."""
    if not program_id and not task_id:
        raise ToolError("get_events requires program_id or task_id")
    events = store.list_events(
        program_id=program_id, task_id=task_id, since_id=since_id, limit=limit
    )
    return json.dumps([e.to_dict() for e in events], indent=2)


@mcp.tool
async def list_tasks(
    status: Optional[Literal["pending", "running", "done", "failed", "cancelled"]] = None,
    kind: Optional[Literal["implement_feature", "fix_bug", "review_repository", "onboard"]] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 20,
) -> str:
    """List recent tasks, most-recent first. Optionally filter by status or kind."""
    tasks = store.list_tasks(status=status, kind=kind, limit=limit)
    return json.dumps([t.to_dict() for t in tasks], indent=2)


@mcp.tool
async def get_scorecard_metrics(
    window_hours: Annotated[int, Field(ge=1, le=24 * 30)] = 168,
) -> str:
    """L8 rolling scorecard: merge rate, evaluator verdict distribution, steer
    rate, first-pass hit rate, workspace-break count — computed over the last
    ``window_hours`` (default 168 = one week). Reads state_store directly, so
    it's cheap and can be called from Telegram or a dashboard without waking
    the goal loop. See ``plan.md`` §Measurement direction for how the numbers
    relate to the C1-C8 production-ready scorecard."""
    from ..telemetry import compute_scorecard
    return json.dumps(compute_scorecard(store, window_hours=int(window_hours)), indent=2)


# ===== cancellation (deliberate abort) =======================================


@mcp.tool
async def cancel_task(task_id: str) -> str:
    """Abort a running or pending task. Tears down its sandbox and marks it
    'cancelled' (a terminal state distinct from 'failed' — it won't be retried or
    resurrected on restart). Cancelling a task that belongs to a program also
    stops that program. No-op if the task already finished. Returns whether an
    abort actually happened."""
    if not task_id:
        raise ToolError("cancel_task requires task_id")
    if not store.get_task(task_id):
        raise ToolError(f"unknown task_id: {task_id}")
    cancelled = queue.cancel_task(task_id)
    return json.dumps(
        {"task_id": task_id, "cancelled": cancelled, "status": "cancelled" if cancelled else None},
        indent=2,
    )


@mcp.tool
async def cancel_program(program_id: str) -> str:
    """Abort a whole program (a start_program goal or an approved build): stop
    scheduling new tasks, tear down every running task's sandbox, and mark the
    program 'cancelled'. Use this as the kill switch for a long or runaway build.
    No-op if the program already terminated. Returns whether an abort happened."""
    if not program_id:
        raise ToolError("cancel_program requires program_id")
    if not store.get_program(program_id):
        raise ToolError(f"unknown program_id: {program_id}")
    cancelled = queue.cancel_program(program_id)
    return json.dumps(
        {"program_id": program_id, "cancelled": cancelled, "status": "cancelled" if cancelled else None},
        indent=2,
    )


# ===== scope grill (waiter-side conversation, chef-side craft) ===============
# The OpenClaw devclaw waiter holds the Telegram conversation; this tool gives it
# the chef's craft — *which* questions matter for a software scope and what 'good'
# looks like. The waiter calls scope_grill each turn with the running transcript;
# the chef returns the next question (with a recommended answer) or, when enough
# is shared, the finalized spec. Stateless: the waiter owns the transcript and,
# once 'done' lands, calls create_goal(spec=...) to file the order.


@mcp.tool
async def scope_grill(
    idea: str,
    transcript: Optional[list[dict]] = None,
) -> str:
    """Take one turn of a scope-alignment grill with the OpenClaw waiter. Given a
    rough project ``idea`` and the ``transcript`` so far (a list of turns each
    with question/recommended/answer), return either the next question to ask
    the customer or the finalized spec when enough is shared.

    The waiter is expected to keep the transcript across turns (it lives in the
    Telegram chat), pass it back unchanged on each call, and append the user's
    reply to the last turn before the next call. This is a stateless cognition
    call — the chef stores nothing here. When the response is ``{"action":
    "done", "spec": ...}``, the waiter calls ``create_goal(..., spec=<spec>)``
    to file the order.

    Response shape:
      {"action": "ask", "question": "<next q>", "recommended": "<your default>"}
      {"action": "done", "spec": "<full spec.md markdown>"}
    """
    if not idea or not idea.strip():
        raise ToolError("scope_grill requires a non-empty idea")
    transcript = transcript or []
    try:
        step = await _elicitation.next_step(idea, transcript)
    except Exception as err:  # noqa: BLE001 — surface as a tool error, not a crash
        raise ToolError(f"scope_grill failed: {err}")
    return json.dumps(step, indent=2)


# ===== dry cognition (test the rail without filing a goal) ===================
# The customer wants to *think about* a project — grill it, see the world-research
# brief, see the decomposition, see how the evaluator would grade the finished
# thing — WITHOUT committing to workspace_dir / repo_url / a persisted goal. These
# tools expose the exact cognition modules the chef runs during a real goal's
# lifecycle, but each one is one-shot and pure: it constructs a throwaway in-memory
# ``Goal``, runs the module's ``default_caller`` (same model tier as production),
# and returns the artifact. Zero writes to /var/lib/devclaw/goals/. Zero admission.


def _dry_goal(
    *,
    objective: str,
    done_when: str = "",
    backlog: Optional[list[str]] = None,
    stub_acceptable: Optional[list[str]] = None,
):
    """Build a throwaway :class:`Goal` for the dry-cognition tools. Persistence
    fields (``workspace_dir``, ``repo_url``, ``verify_cmd``) get harmless
    placeholders — the dry tools NEVER touch disk or clone, and the cognition
    modules only read the fields the prompts actually reference."""
    from ..goal.models import Goal

    return Goal(
        id="dry-run",
        objective=objective,
        cadence="1d",
        engine="devclaw",
        workspace_dir="/dev/null",
        repo_url=None,
        verify_cmd=None,
        open_pr=False,
        done_when=done_when,
        backlog=backlog or [],
        stub_acceptable=stub_acceptable or [],
        skills_required=[],
    )


@mcp.tool
async def dry_world_research(
    objective: str,
    spec: str = "",
    done_when: str = "",
) -> str:
    """PURE COGNITION — no goal filed, no workspace, no side effects.

    Runs the world-research brief the chef fires at investigation-open time for a
    from-scratch goal (the same module used when ``repo_url`` is absent). Returns
    the ``## Real-world exemplars`` / ``## What good MVP looks like`` / ``##
    Deliberately defer`` brief as markdown. Use this to test the harness's read
    of "build me X" ideas without filing a real goal — no workspace or repo URL
    required.

    Inputs:
      objective: the durable aim (e.g., "build a CRM for SMB sales teams").
      spec: optional aligned spec markdown (e.g., what ``scope_grill`` returned).
      done_when: optional completion contract if you have one.
    """
    if not objective or not objective.strip():
        raise ToolError("dry_world_research requires a non-empty objective")
    from ..goal import world_research as _world

    goal = _dry_goal(objective=objective, done_when=done_when)
    try:
        return await _world.world_brief(goal, spec, caller=_world.default_caller())
    except Exception as err:  # noqa: BLE001
        raise ToolError(f"dry_world_research failed: {err}")


@mcp.tool
async def dry_decompose(
    objective: str,
    spec: str = "",
    done_when: str = "",
    backlog: Optional[list[str]] = None,
    discovery_brief: str = "",
    repo_digest: str = "",
    stub_acceptable: Optional[list[str]] = None,
) -> str:
    """PURE COGNITION — no goal filed, no workspace, no side effects.

    Runs the goal decomposer against an in-memory objective and returns the
    CHECKLIST YAML it would persist to ``checklist.yaml``. Use this to test how
    the harness would break a goal into milestones + atomic items with
    per-item ``evidence_target``/``depends_on`` — without filing a real goal.

    Inputs:
      objective / done_when / backlog / stub_acceptable — the goal facts the
        prompt reads verbatim.
      discovery_brief: optional prior pass (e.g., dry_world_research output) so
        the decomposer plans against a real-world MVP shape.
      repo_digest: optional curated repo excerpt — passing one exercises the
        existing-repo decomposition path; leaving it empty exercises the
        from-scratch path.
    """
    if not objective or not objective.strip():
        raise ToolError("dry_decompose requires a non-empty objective")
    from ..goal import decomposer as _decomp
    from ..goal.checklist import dump_checklist

    goal = _dry_goal(
        objective=objective, done_when=done_when, backlog=backlog,
        stub_acceptable=stub_acceptable,
    )
    try:
        checklist = await _decomp.decompose(
            goal,
            claude_caller=_decomp.default_caller(),
            discovery_brief=discovery_brief,
            repo_digest=repo_digest,
        )
    except Exception as err:  # noqa: BLE001
        raise ToolError(f"dry_decompose failed: {err}")
    return dump_checklist(checklist)


@mcp.tool
async def dry_evaluate(
    objective: str,
    done_when: str,
    review_report: str,
    spec: str = "",
    backlog: Optional[list[str]] = None,
    stub_acceptable: Optional[list[str]] = None,
    deliveries: str = "",
    recent_log: str = "",
    at_done_gate: bool = True,
) -> str:
    """PURE COGNITION — no goal filed, no workspace, no side effects.

    Runs the direction evaluator (the cognition that grades a goal at the
    done-gate) against hypothetical inputs and returns the JSON verdict:
    ``{verdict, rationale, corrections, question, clauses}``. Use this to
    sanity-check the harness's judgement on "here's what shipped vs. what was
    asked" — including whether it would refuse stub-disguise on a specific
    review — without touching a real goal.

    Defaults to ``at_done_gate=True`` (strict per-clause grading, the mode the
    real done-gate runs). Pass a ``review_report`` shaped like a
    ``review_repository`` task's output (``## Per-clause evidence`` +
    ``## Structural health`` sections) to exercise the full done-gate path.
    """
    if not objective or not objective.strip():
        raise ToolError("dry_evaluate requires a non-empty objective")
    if not done_when or not done_when.strip():
        raise ToolError("dry_evaluate requires done_when (the completion contract)")
    from dataclasses import asdict

    from ..goal import evaluator as _eval
    from ..goal.models import GoalStatus

    goal = _dry_goal(
        objective=objective, done_when=done_when, backlog=backlog,
        stub_acceptable=stub_acceptable,
    )
    status = GoalStatus(phase="done" if at_done_gate else "in_flight")
    try:
        result = await _eval.evaluate(
            goal, status, recent_log, deliveries,
            claude_caller=_eval.default_caller(),
            review_report=review_report or None,
            at_done_gate=at_done_gate,
            spec=spec,
        )
    except Exception as err:  # noqa: BLE001
        raise ToolError(f"dry_evaluate failed: {err}")
    return json.dumps(asdict(result), indent=2)


# ===== goal layer (durable, steerable, evaluated goals) ======================
# The folded-in goalclaw. A `program` is a bounded, one-shot DAG; a `goal` is an
# open-ended standing intent that DevClaw drives across many heartbeats —
# planning the next action, dispatching it into the queue, and EVALUATING whether
# the work is actually moving toward the objective (not just shipping PRs). These
# tools are the steer/observe surface: ask what's going on, correct it.


@mcp.tool
async def create_goal(
    goal_id: str,
    objective: str,
    workspace_dir: str,
    done_when: str = "",
    backlog: Optional[list[str]] = None,
    cadence: str = "1d",
    repo_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = True,
    spec: str = "",
    skills_required: Optional[list[str]] = None,
) -> str:
    """Register a DURABLE goal that DevClaw drives over time. Unlike start_program
    (a one-shot DAG that runs to completion), a goal persists: on each heartbeat
    DevClaw plans the single next action, dispatches it to the engine, records what
    shipped, and periodically EVALUATES whether the work is actually achieving the
    objective — only closing the goal when a grounded review confirms done_when is
    met. Steer it any time with steer_goal; inspect it with get_goal.

    goal_id: a short stable slug (the on-disk folder name). objective: the durable
    aim. done_when: the prose completion test the evaluator judges against. backlog:
    a starting work-list. workspace_dir: the repo checkout DevClaw keeps fresh per
    action; repo_url clones it if absent. verify_cmd: the gate (e.g. 'dotnet test').
    spec: optional pre-aligned scope contract — when the OpenClaw waiter has
    grilled the customer (via scope_grill) before filing the order, pass the
    finalized spec.md here and the evaluator judges done against it."""
    if not goal_id:
        raise ToolError("create_goal requires goal_id")
    # objective + workspace_dir are checked inside admission and surfaced as
    # structured conditions — don't duplicate them here.
    from ..goal.admission import GoalAdmissionRejected

    try:
        return json.dumps(
            goals.create_goal(
                goal_id, objective=objective, workspace_dir=workspace_dir,
                done_when=done_when, backlog=backlog, cadence=cadence,
                repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
                spec=spec, skills_required=skills_required,
            ),
            indent=2,
        )
    except FileExistsError:
        raise ToolError(f"goal {goal_id!r} already exists")
    except GoalAdmissionRejected as exc:
        # Structured rejection: surface the full condition list so the waiter
        # can render fixable items to the customer and route on the codes.
        raise ToolError(json.dumps(exc.result.to_dict(), indent=2))


@mcp.tool
async def verify_goal(
    objective: str,
    workspace_dir: str,
    done_when: str = "",
    backlog: Optional[list[str]] = None,
    repo_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    spec: str = "",
    skills_required: Optional[list[str]] = None,
) -> str:
    """Pre-flight check for a goal BEFORE you call create_goal. Runs the same
    structural validations the chef applies at goal-creation time and returns
    a list of conditions (severity ``reject`` or ``warn``) with machine-readable
    codes the waiter can route on.

    Use this to preview rejections so the customer sees fixable conditions
    before they think the order was filed. ``admitted: false`` means
    create_goal would reject; ``admitted: true`` with warnings means
    create_goal would accept but flag.

    Response shape:
      {"admitted": bool,
       "conditions": [{"code": "...", "severity": "reject"|"warn",
                       "message": "...", "field": "..."}, ...]}
    """
    return json.dumps(
        goals.verify_goal(
            objective=objective, workspace_dir=workspace_dir, done_when=done_when,
            backlog=backlog, repo_url=repo_url, verify_cmd=verify_cmd, spec=spec,
            skills_required=skills_required,
        ),
        indent=2,
    )


@mcp.tool
async def get_goal(goal_id: str) -> str:
    """Inspect a durable goal: its objective + done_when, current phase, what's
    in flight, the last direction-evaluation verdict, and the recent log. This is
    the 'what's going on / what direction' surface."""
    try:
        return json.dumps(goals.get_goal(goal_id), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def tail_goal(
    goal_id: str,
    log_lines: int = 40,
    deliveries_chars: int = 6000,
    event_limit: int = 30,
) -> str:
    """Watch a goal run — the deep, read-only observability surface. Beyond
    get_goal's phase/direction it returns the grounded deliveries tail (what each
    action actually shipped: agent summary + gate verdict + PR), the discovery
    brief + any pre-aligned spec, and the tail of the LIVE event stream from
    whatever task is in flight — so you can see the engineer acting in near real
    time without SSHing to the box. Everything is bounded; call repeatedly to
    follow progress."""
    if not goal_id:
        raise ToolError("tail_goal requires goal_id")
    try:
        return json.dumps(
            goals.tail_goal(
                goal_id,
                log_lines=log_lines,
                deliveries_chars=deliveries_chars,
                event_limit=event_limit,
            ),
            indent=2,
        )
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def list_goals() -> str:
    """List all durable goals with their phase + latest direction verdict."""
    return json.dumps(goals.list_goals(), indent=2)


@mcp.tool
async def steer_goal(goal_id: str, message: str) -> str:
    """Correct or redirect a durable goal. The message is recorded as steering and
    the next-action planner honors it over the backlog on the next tick (which is
    poked immediately). Unblocks a blocked goal. Use to change direction, add work,
    or answer what a goal is blocked on — e.g. 'use Postgres, not SQLite' or
    'skip the admin UI, focus on the API'."""
    if not goal_id or not message:
        raise ToolError("steer_goal requires goal_id and message")
    try:
        return json.dumps(goals.steer_goal(goal_id, message), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def resume_goal(goal_id: str) -> str:
    """Resume a BLOCKED goal whose blocker has been cleared out-of-band — the
    recovery verb. Fires the goal's existing UNBLOCK transition and forces a
    re-plan on the next heartbeat tick (poked immediately), re-attempting the
    SAME contract: no steering is recorded and the objective/done_when/backlog
    are untouched. This does NOT change direction (use steer_goal for that)
    and is NOT a field-patch/update tool — nothing about the goal is edited.

    Idempotent: on a goal that is not blocked it no-ops with a message. A goal
    blocked in FIRMING (awaiting owner answers) is refused — those answers can
    only come through answer_unknowns."""
    if not goal_id:
        raise ToolError("resume_goal requires goal_id")
    try:
        return json.dumps(goals.resume_goal(goal_id), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def evaluate_goal(goal_id: str) -> str:
    """Force an on-demand direction evaluation NOW, grounded in the goal's
    artifacts. Reads recent deliveries + log + spec, runs the evaluator, and
    returns the fresh verdict (``on_track`` / ``off_track`` / ``achieved`` /
    ``stalled`` / ``needs_human``) with the evaluator's rationale. Any
    corrections are appended to the goal's ``inbox.md`` as steering (the
    next-action planner picks them up) AND the heartbeat is poked.

    Distinct from the per-tick evaluator (which runs on cadence inside the
    heartbeat) — this is the surface the owner OR the operations agent calls
    to wake a stuck goal, get a fresh direction read, or ground a
    "should I close this?" decision in evidence on demand.

    Returns::

        {"goal_id": "...", "verdict": "...", "rationale": "...",
         "corrections": [...], "question": "..."}
    """
    if not goal_id:
        raise ToolError("evaluate_goal requires goal_id")
    try:
        return json.dumps(await goals.evaluate_goal(goal_id), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def answer_unknowns(goal_id: str, answers: dict[str, str]) -> str:
    """Answer the open ``unknowns`` from a goal's firmed-draft so DevClaw can
    finish firming and start work. Synchronous: fires firming round N+1 inside
    the call, then either transitions the goal to executing (decomposer fires
    on the next heartbeat) or returns a new set of unknowns that the prior
    answers exposed.

    ``answers`` MUST cover EVERY currently open unknown id exactly once — no
    partials, no extras. Get the current unknowns from ``get_goal(goal_id)``'s
    ``firmed_draft.unknowns`` field; pass them as ``{unknown_id: answer_text}``.
    The waiter is responsible for collecting a complete answer set from the
    owner before calling this tool.

    Response:
      {"status": "firmed", "round": N, "unknowns": []}
      {"status": "needs_more_answers", "round": N, "unknowns": [{...}, ...]}
    """
    if not goal_id:
        raise ToolError("answer_unknowns requires goal_id")
    if not isinstance(answers, dict) or not answers:
        raise ToolError("answer_unknowns requires a non-empty answers map")
    try:
        result = await goals.answer_unknowns(goal_id, answers)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")
    except ValueError as exc:
        raise ToolError(str(exc))
    return json.dumps(result, indent=2)


@mcp.tool
async def get_trace(
    goal_id: str,
    since_id: int = 0,
    limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    kind: Optional[str] = None,
) -> str:
    """Read durable trace events for a goal — every cognition call, dispatch,
    delivery, subprocess, and notification a heartbeat tick has emitted, in
    emission order. Grouped by ``trace_id`` (one per goal-tick).

    Use this to inspect what actually happened during a cascade: which prompts
    fired with what role, how long each cognition call took, real input/output
    tokens + cost from the CLI's usage envelope (``tokens_in``/``tokens_out``/
    ``cost_usd``; legacy rows and fallback calls carry only the ``_est`` len/4
    estimates — labeled as estimates), the FULL response text, and the chain of
    dispatches that followed. Goal-scoped cognition rows also carry
    ``transcript_file`` — the full prompt+response transcript under the goal
    dir's ``transcripts/``. Pair with ``get_goal`` for the high-level state +
    this for the causal detail.

    Returns ``{"events": [...], "totals": {...}}``. Totals prefer real tokens
    per row and report ``cognition_rows_estimated`` for how many rows fell back
    to estimates. Pass ``since_id`` (the monotonic id of the last event you've
    seen) to incrementally tail; pass ``kind`` to filter (e.g. ``cognition``
    for prompts only).
    """
    if not goal_id:
        raise ToolError("get_trace requires goal_id")
    events = store.read_traces(
        goal_id=goal_id, since_id=since_id, limit=limit, kind=kind,
    )
    totals = store.trace_totals(goal_id=goal_id)
    return json.dumps({"events": events, "totals": totals}, indent=2, default=str)


@mcp.tool
async def cancel_goal(goal_id: str) -> str:
    """Permanently stop a durable goal. Sets its phase to 'cancelled' (a terminal
    state — DevClaw will skip it on every future heartbeat) and tears down any
    in-flight task or program associated with it. Returns a graceful no-op response
    if the goal is already in a terminal phase (done or cancelled) — safe to call
    more than once. Use when you no longer want DevClaw to drive a goal."""
    if not goal_id:
        raise ToolError("cancel_goal requires goal_id")
    try:
        return json.dumps(goals.cancel_goal(goal_id), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


# ===== build a project from scratch ==========================================


@mcp.tool
async def create_repo(
    name: str,
    private: bool = False,
    description: str = "",
) -> str:
    """Create a fresh GitHub repo under the configured account so a from-scratch
    goal has somewhere to live. Returns {created, existed, repo, clone_url}. The
    repo is seeded with a README (initial commit + a 'main' default branch) so it
    can be cloned and PR'd against immediately. Idempotent: if the name already
    exists it returns that repo instead of failing. Feed the returned clone_url
    into create_goal(repo_url=...). Auth is gh's own login (repo write access).
    Public by default — Actions on private repos never start under the
    account's billing lock; pass private=true only for sensitive content."""
    if not name:
        raise ToolError("create_repo requires a name")
    try:
        return json.dumps(
            await _repo.create_repo(name, private=private, description=description),
            indent=2,
        )
    except _repo.RepoError as err:
        raise ToolError(str(err))


# ===== durable deploy hosting ================================================
# Long-lived, reboot-surviving container at a STABLE per-slug URL over Tailscale.
# Auto-fires when a goal reaches `achieved` (see goal_tick). See devclaw/deploy.py.


@mcp.tool
async def deploy_project(workspace_dir: str, slug: str) -> str:
    """Deploy a project's BUILT app as a DURABLE host on the VPS and return its stable
    Tailscale URL — so the owner is HANDED a running product to open, not a diff to
    read. Survives reboots (--restart unless-stopped), lives at a fixed per-slug
    port so the URL never changes across redeploys, and is reachable over Tailscale
    (https, auto-TLS, never public). Idempotent: redeploying the same slug replaces
    the container at the same URL. workspace_dir = the goal's checkout; slug = a
    short stable name."""
    if not workspace_dir or not slug:
        raise ToolError("deploy_project requires workspace_dir and slug")
    try:
        return json.dumps(await _deploy.deploy_project(workspace_dir, slug), indent=2)
    except _deploy.DeployError as err:
        raise ToolError(str(err))


@mcp.tool
async def deploy_status(slug: str) -> str:
    """Status of a durable deploy: whether it exists, is running, is answering
    (ready), its stable Tailscale URL, and the one-time serve command."""
    if not slug:
        raise ToolError("deploy_status requires slug")
    return json.dumps(await _deploy.deploy_status(slug), indent=2)


@mcp.tool
async def stop_deploy(slug: str) -> str:
    """Stop and remove a durable deploy, tear down its Tailscale serve, and free its
    VPS resources."""
    if not slug:
        raise ToolError("stop_deploy requires slug")
    return json.dumps(await _deploy.stop_deploy(slug), indent=2)


@mcp.tool
async def list_deploys() -> str:
    """List all durable deploys (running + stopped) with their status."""
    return json.dumps(await _deploy.list_deploys(), indent=2)


# ===== project registry (control plane) ======================================
# The portfolio view: which repos devclaw owns + the live status of each. The
# registry links repos to their driving goals; status is joined live.


@mcp.tool
async def register_project(
    project_id: str,
    name: str,
    repo_url: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    preview_url: Optional[str] = None,
    notes: str = "",
    automerge: Optional[Literal["on", "off"]] = None,
    merge_strategy: Optional[Literal["squash", "merge", "rebase"]] = None,
    autodeploy: Optional[Literal["on", "off"]] = None,
    review_gate: Optional[Literal["on", "off"]] = None,
    verify_done: Optional[Literal["on", "off"]] = None,
) -> str:
    """Register a repo in the project registry — the control plane's source of
    truth for 'what is devclaw working on'. ``project_id`` is a stable slug (e.g.
    'todo-fullstack-demo'). Link the goal(s) driving it with link_goal. Idempotent
    failure: a taken id is an error (use update_project to change it).

    Per-project override knobs (each overrides its devclaw-wide env default;
    omit to inherit — the usual choice). This is the ONLY place these are
    configured per repo; a goal itself carries none of them:
      - ``automerge`` — auto-merge gate-passed PRs (DEVCLAW_GOAL_AUTOMERGE).
      - ``merge_strategy`` — squash|merge|rebase for the merge (DEVCLAW_GOAL_MERGE_STRATEGY).
      - ``autodeploy`` — deploy on goal completion (devclaw default: on).
      - ``review_gate`` — run the pre-PR review gate (devclaw default: on).
      - ``verify_done`` — grounded done-gate re-check before closing (devclaw default: on)."""
    if not project_id or not name:
        raise ToolError("register_project requires project_id and name")
    _onoff = {"on": True, "off": False}
    try:
        p = registry.create(
            id=project_id, name=name, repo_url=repo_url,
            workspace_dir=workspace_dir, preview_url=preview_url, notes=notes,
            automerge=(None if automerge is None else automerge == "on"),
            merge_strategy=merge_strategy,
            autodeploy=(None if autodeploy is None else _onoff[autodeploy]),
            review_gate=(None if review_gate is None else _onoff[review_gate]),
            verify_done=(None if verify_done is None else _onoff[verify_done]),
        )
    except ProjectExists:
        raise ToolError(f"project already exists: {project_id}")
    return json.dumps(p.to_dict(), indent=2)


@mcp.tool
async def list_projects(status: Optional[str] = None) -> str:
    """List registered projects with a live status rollup (each project's linked
    goals' phase/direction + a derived health: working/blocked/done/idle/archived).
    Filter by status (active|paused|archived). This is the 'show me everything'
    surface for chat / API / CLI."""
    all_goals = goals.list_goals()
    items = [
        project_rollup(p, all_goals)
        for p in registry.list(status=status)  # type: ignore[arg-type]
    ]
    return json.dumps(items, indent=2)


@mcp.tool
async def project_status(project_id: str) -> str:
    """Full status of one registered project: its facts (repo, workspace, preview)
    plus the LIVE status of every goal driving it and a derived health signal."""
    p = registry.get(project_id)
    if p is None:
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps(project_rollup(p, goals.list_goals()), indent=2)


@mcp.tool
async def update_project(
    project_id: str,
    name: Optional[str] = None,
    repo_url: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    preview_url: Optional[str] = None,
    status: Optional[Literal["active", "paused", "archived"]] = None,
    notes: Optional[str] = None,
    automerge: Optional[Literal["on", "off", "inherit"]] = None,
    merge_strategy: Optional[Literal["squash", "merge", "rebase", "inherit"]] = None,
    autodeploy: Optional[Literal["on", "off", "inherit"]] = None,
    review_gate: Optional[Literal["on", "off", "inherit"]] = None,
    verify_done: Optional[Literal["on", "off", "inherit"]] = None,
) -> str:
    """Update a registered project's facts — only the fields you pass change. Use to
    record a preview URL, pause/archive it, or correct the repo/workspace.

    Per-project override knobs — ``automerge`` / ``merge_strategy`` /
    ``autodeploy`` / ``review_gate`` / ``verify_done`` — each take a concrete
    value to PIN this project (overriding its devclaw-wide env default),
    'inherit' to CLEAR a prior override back to that default, or omit to leave
    whatever is currently set untouched. (bool knobs take 'on'/'off';
    merge_strategy takes 'squash'/'merge'/'rebase'.)"""
    override_kwargs: dict = {}
    _onoff = {"on": True, "off": False, "inherit": None}
    for field, val in (("automerge", automerge), ("autodeploy", autodeploy),
                       ("review_gate", review_gate), ("verify_done", verify_done)):
        if val is not None:
            override_kwargs[field] = _onoff[val]
    if merge_strategy is not None:
        override_kwargs["merge_strategy"] = None if merge_strategy == "inherit" else merge_strategy
    try:
        p = registry.update(
            project_id, name=name, repo_url=repo_url, workspace_dir=workspace_dir,
            preview_url=preview_url, status=status, notes=notes,
            **override_kwargs,
        )
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps(p.to_dict(), indent=2)


_TERMINAL_GOAL_PHASES = {"done", "cancelled", "error", "achieved"}


def _project_active_goal_ids(project) -> list[str]:
    """All non-terminal goal ids that belong to this project — by workspace_dir
    match (the authoritative join) OR by the advisory ``goal_ids`` list.

    Used by the one-goal-per-project warn (2026-07-04): both entry points
    (create_goal against a matching workspace, or link_goal directly) count
    toward the "already-has-active-goal" state."""
    from ..project_registry import _normalize_workspace

    proj_ws = _normalize_workspace(project.workspace_dir)
    seen: set[str] = set()
    active: list[str] = []
    for g in goals.list_goals():
        gid = g.get("id")
        if not gid:
            continue
        if g.get("phase") in _TERMINAL_GOAL_PHASES:
            continue
        matches_ws = (
            proj_ws is not None
            and _normalize_workspace(g.get("workspace_dir")) == proj_ws
        )
        matches_link = gid in (project.goal_ids or [])
        if matches_ws or matches_link:
            if gid not in seen:
                seen.add(gid)
                active.append(gid)
    return active


@mcp.tool
async def link_goal(project_id: str, goal_id: str, unlink: bool = False) -> str:
    """Attach (or, with unlink=True, detach) a durable goal to/from a project. The
    link is by id only — the goal's status is joined live in list_projects /
    project_status, never copied. Idempotent.

    Warn-first one-goal-per-project (2026-07-04): if the project already has
    an active goal, linking a second one still succeeds but the response
    carries a ``warning`` field and the console renders a banner. Hard reject
    lands in a follow-up PR after the warn phase has bake time. Under the
    standing rule, a project pursues one well-defined goal at a time — if
    you need a new direction, cancel + refile."""
    try:
        p = (
            registry.unlink_goal(project_id, goal_id)
            if unlink
            else registry.link_goal(project_id, goal_id)
        )
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    out = p.to_dict()
    if not unlink:
        other_active = [gid for gid in _project_active_goal_ids(p) if gid != goal_id]
        if other_active:
            out["warning"] = {
                "code": "multiple_active_goals",
                "message": (
                    "This project already has "
                    f"{len(other_active)} active goal(s): "
                    f"{', '.join(other_active)}. Under the one-goal-per-project "
                    "rule (2026-07-04) a project pursues one goal at a time — "
                    "cancel + refile instead of stacking. This will become a "
                    "hard error after the warn-first phase."
                ),
                "otherActiveGoalIds": other_active,
            }
    return json.dumps(out, indent=2)


@mcp.tool
async def delete_project(project_id: str) -> str:
    """Permanently remove a project from the registry (HARD delete). Drops only the
    registry record — the goals it linked are untouched (they live on disk and are
    just unlinked from this view). To retire a project while keeping its record + a
    paper trail, prefer update_project(status='archived'). Raises if the id is
    unknown (so a typo doesn't silently no-op)."""
    if not registry.delete(project_id):
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps({"deleted": True, "project_id": project_id}, indent=2)
