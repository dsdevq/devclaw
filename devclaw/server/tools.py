"""All MCP tool decorators — the chef's menu.

Each tool delegates to the long-lived services in ``_state`` (queue, store,
goals, registry) or to a sibling module (``deploy``, ``repo``). The tools stay
thin on purpose: validate inputs, dispatch, return JSON. Cognition lives below
(planner / evaluator / review gate), not here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import deploy as _deploy
from .. import elicitation as _elicitation
from .. import repo as _repo
from ..project_registry import ProjectExists, project_rollup
from ._state import _goal_get, goals, mcp, queue, registry, store


@mcp.tool
async def implement_feature(
    workspace_dir: str,
    goal: str,
    notify_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = False,
) -> str:
    """Submit a natural-language coding goal to be executed by OpenHands in the
    given workspace_dir. Returns a task_id immediately; the task runs
    asynchronously. Poll get_status(task_id), or pass notify_url to be pushed
    the result. Use for new features / open-ended changes; prefer fix_bug for an
    existing defect, and review_repository for a read-only review.

    Pass verify_cmd (e.g. "dotnet test", "npm run build && npm run test:ci") to
    gate the task: after the agent finishes, DevClaw runs that command in the
    workspace and the task only succeeds if it exits 0 — the agent's own
    "I'm done" is not trusted. A failing gate marks the task failed with the
    command output captured.

    Pass open_pr=True to DELIVER a successful change as something you review: on
    `done`, DevClaw commits it to a branch, pushes, and opens a PR (best-effort;
    needs git push auth + a GitHub remote), recording the PR URL on the task."""
    if not workspace_dir or not goal:
        raise ToolError("implement_feature requires workspace_dir and goal")
    task_id = queue.submit(
        kind="implement_feature",
        workspace_dir=workspace_dir,
        goal=goal,
        notify_url=notify_url,
        verify_cmd=verify_cmd,
        deliver=open_pr,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


@mcp.tool
async def fix_bug(
    workspace_dir: str,
    description: str,
    notify_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    open_pr: bool = False,
) -> str:
    """Submit a bug-fix task. Like implement_feature, but with a prompt that
    biases OpenHands toward reading existing code first, making the smallest
    fix, not refactoring unrelated code, and running the tests. Returns task_id
    immediately. Same optional notify_url as implement_feature.

    Pass verify_cmd (e.g. the repo's test command) to gate the fix: DevClaw runs
    it after the agent finishes and only marks the task done if it exits 0.
    Pass open_pr=True to deliver a successful fix as a branch/PR you review."""
    if not workspace_dir or not description:
        raise ToolError("fix_bug requires workspace_dir and description")
    task_id = queue.submit(
        kind="fix_bug",
        workspace_dir=workspace_dir,
        goal=description,
        notify_url=notify_url,
        verify_cmd=verify_cmd,
        deliver=open_pr,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


@mcp.tool
async def review_repository(
    workspace_dir: str, focus: str = "", notify_url: Optional[str] = None
) -> str:
    """Submit a READ-ONLY code review task. OpenHands inspects the workspace and
    writes a review report; it is prompt-instructed NOT to modify any files.
    Returns task_id immediately; the report appears in the task's result_json
    agent_output once status=done. Same optional notify_url as implement_feature."""
    if not workspace_dir:
        raise ToolError("review_repository requires workspace_dir")
    task_id = queue.submit(
        kind="review_repository",
        workspace_dir=workspace_dir,
        goal=focus or "general code review",
        notify_url=notify_url,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


# ===== setup_cicd (pure mechanism — no cognition) ============================


def _detect_stack(workspace: Path) -> str:
    """Return a coarse stack label used to pick a CI template."""
    if any(workspace.glob("**/*.csproj")):
        return "dotnet"
    if (workspace / "pyproject.toml").exists() or (workspace / "setup.py").exists():
        return "python"
    if (workspace / "package.json").exists():
        return "node"
    if (workspace / "go.mod").exists():
        return "go"
    return "generic"


_CI_TEMPLATES: dict[str, str] = {
    "dotnet": textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: [main]
          pull_request:
          workflow_dispatch:
        jobs:
          build:
            runs-on: self-hosted
            concurrency:
              group: ci-${{ github.ref }}
              cancel-in-progress: true
            steps:
              - uses: actions/checkout@v4
              - name: Build
                run: dotnet build --configuration Release
              - name: Test
                run: dotnet test --configuration Release --no-build
        """),
    "python": textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: [main]
          pull_request:
          workflow_dispatch:
        jobs:
          build:
            runs-on: self-hosted
            concurrency:
              group: ci-${{ github.ref }}
              cancel-in-progress: true
            steps:
              - uses: actions/checkout@v4
              - name: Install
                run: pip install -e ".[dev]" --quiet
              - name: Test
                run: pytest
        """),
    "node": textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: [main]
          pull_request:
          workflow_dispatch:
        jobs:
          build:
            runs-on: self-hosted
            concurrency:
              group: ci-${{ github.ref }}
              cancel-in-progress: true
            steps:
              - uses: actions/checkout@v4
              - name: Install
                run: npm ci
              - name: Build
                run: npm run build --if-present
              - name: Test
                run: npm test --if-present
        """),
    "generic": textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: [main]
          pull_request:
          workflow_dispatch:
        jobs:
          build:
            runs-on: self-hosted
            concurrency:
              group: ci-${{ github.ref }}
              cancel-in-progress: true
            steps:
              - uses: actions/checkout@v4
              - name: Smoke check
                run: echo "CI placeholder — replace with real build/test steps"
        """),
}
# go uses the generic template for now
_CI_TEMPLATES["go"] = _CI_TEMPLATES["generic"].replace(
    "CI placeholder — replace with real build/test steps",
    "go build ./... && go test ./...",
)


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _cicd_setup_sync(workspace_dir: str) -> dict:
    """Pure-mechanism CI/CD bootstrap. No cognition, no OpenHands.

    Returns a dict with keys: status (present|created|error), detail (str).
    """
    ws = Path(workspace_dir)
    workflows_dir = ws / ".github" / "workflows"

    if workflows_dir.exists() and any(workflows_dir.glob("*.yml")):
        existing = [p.name for p in workflows_dir.glob("*.yml")]
        return {"status": "present", "detail": f"CI already configured: {existing}"}

    # Detect stack and pick template
    stack = _detect_stack(ws)
    template = _CI_TEMPLATES[stack]

    # Write the workflow
    workflows_dir.mkdir(parents=True, exist_ok=True)
    ci_path = workflows_dir / "ci.yml"
    ci_path.write_text(template)

    # Commit and push
    r = _run(["git", "add", ".github/workflows/ci.yml"], workspace_dir)
    if r.returncode != 0:
        return {"status": "error", "detail": f"git add failed: {r.stderr.strip()}"}

    r = _run(
        ["git", "commit", "-m", f"ci: add self-hosted {stack} CI workflow\n\nGenerated by devclaw setup_cicd."],
        workspace_dir,
    )
    if r.returncode != 0:
        return {"status": "error", "detail": f"git commit failed: {r.stderr.strip()}"}

    r = _run(["git", "push"], workspace_dir)
    if r.returncode != 0:
        return {"status": "error", "detail": f"git push failed: {r.stderr.strip()}"}

    return {
        "status": "created",
        "detail": f"Created .github/workflows/ci.yml ({stack} template) and pushed.",
        "stack": stack,
        "note": (
            "Workflow uses 'runs-on: self-hosted'. Register a GitHub Actions runner "
            "for this repo on the VPS to activate it."
        ),
    }


@mcp.tool
async def setup_cicd(workspace_dir: str) -> str:
    """Check whether a repository has GitHub Actions CI configured. If not,
    detect the tech stack (dotnet / python / node / go / generic), commit a
    standard self-hosted-runner workflow to .github/workflows/ci.yml, and push
    it. Safe to call multiple times — exits early if CI already exists.

    Note: the workflow targets 'runs-on: self-hosted'. A GitHub Actions runner
    must be registered for the repo on the VPS to pick up jobs. This tool only
    creates the workflow file; runner registration is a separate VPS-side step."""
    if not workspace_dir:
        raise ToolError("setup_cicd requires workspace_dir")
    result = await asyncio.get_event_loop().run_in_executor(
        None, _cicd_setup_sync, workspace_dir
    )
    return json.dumps(result, indent=2)


@mcp.tool
async def onboard(
    workspace_dir: str, focus: str = "", notify_url: Optional[str] = None
) -> str:
    """Onboard a repository: analyze it and write a DRAFT AGENTS.md so future
    tasks start informed. OpenHands inspects the workspace READ-ONLY (it modifies
    no file except the AGENTS.md it writes) and captures COMPREHENSION ONLY —
    stack, layout, how to build/run/test (incl. the command to use as the verify
    gate), conventions, and setup gotchas. Project direction / decision-log are
    deliberately out of scope.

    Human-in-the-loop: the draft is NOT authoritative until you review it. It
    lands in the repo working tree (review it via `git diff`) and the agent's
    summary appears in the task's result_json once status=done. If the repo
    already has an AGENTS.md, the agent validates it against the real repo and
    keeps what's still accurate — only correcting what's wrong or missing —
    rather than clobbering hand-written project memory. Returns task_id
    immediately; same optional notify_url as implement_feature.

    Also automatically runs setup_cicd: if the repo has no GitHub Actions
    workflows, a standard self-hosted-runner CI file is committed and pushed
    before the analysis task starts. The cicd_setup field in the response
    reports what happened (present | created | error)."""
    if not workspace_dir:
        raise ToolError("onboard requires workspace_dir")
    cicd = await asyncio.get_event_loop().run_in_executor(
        None, _cicd_setup_sync, workspace_dir
    )
    task_id = queue.submit(
        kind="onboard",
        workspace_dir=workspace_dir,
        goal=focus or "general onboarding",
        notify_url=notify_url,
    )
    return json.dumps({"task_id": task_id, "status": "pending", "cicd_setup": cicd}, indent=2)


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
    if not goal_id or not objective or not workspace_dir:
        raise ToolError("create_goal requires goal_id, objective, workspace_dir")
    try:
        return json.dumps(
            goals.create_goal(
                goal_id, objective=objective, workspace_dir=workspace_dir,
                done_when=done_when, backlog=backlog, cadence=cadence,
                repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
                spec=spec,
            ),
            indent=2,
        )
    except FileExistsError:
        raise ToolError(f"goal {goal_id!r} already exists")


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
async def answer_goal(goal_id: str, answer: str) -> str:
    """Deliver an owner's reply to a goal that is waiting on them. When a goal is
    in 'plan_review', any reply here approves the plan and execution begins. The
    goal is poked to advance immediately. No-op (with an explanatory result) if
    the goal isn't currently awaiting input. (Scope alignment is held entirely on
    the waiter side via scope_grill — by the time a goal exists, scope is fixed.)"""
    if not goal_id or not answer:
        raise ToolError("answer_goal requires goal_id and answer")
    try:
        return json.dumps(goals.answer_goal(goal_id, answer), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


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
    private: bool = True,
    description: str = "",
) -> str:
    """Create a fresh GitHub repo under the configured account so a from-scratch
    goal has somewhere to live. Returns {created, existed, repo, clone_url}. The
    repo is seeded with a README (initial commit + a 'main' default branch) so it
    can be cloned and PR'd against immediately. Idempotent: if the name already
    exists it returns that repo instead of failing. Feed the returned clone_url
    into create_goal(repo_url=...). Auth is gh's own login (repo write access)."""
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
) -> str:
    """Register a repo in the project registry — the control plane's source of
    truth for 'what is devclaw working on'. ``project_id`` is a stable slug (e.g.
    'todo-fullstack-demo'). Link the goal(s) driving it with link_goal. Idempotent
    failure: a taken id is an error (use update_project to change it)."""
    if not project_id or not name:
        raise ToolError("register_project requires project_id and name")
    try:
        p = registry.create(
            id=project_id, name=name, repo_url=repo_url,
            workspace_dir=workspace_dir, preview_url=preview_url, notes=notes,
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
    items = [
        project_rollup(p, _goal_get)
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
    return json.dumps(project_rollup(p, _goal_get), indent=2)


@mcp.tool
async def update_project(
    project_id: str,
    name: Optional[str] = None,
    repo_url: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    preview_url: Optional[str] = None,
    status: Optional[Literal["active", "paused", "archived"]] = None,
    notes: Optional[str] = None,
) -> str:
    """Update a registered project's facts — only the fields you pass change. Use to
    record a preview URL, pause/archive it, or correct the repo/workspace."""
    try:
        p = registry.update(
            project_id, name=name, repo_url=repo_url, workspace_dir=workspace_dir,
            preview_url=preview_url, status=status, notes=notes,
        )
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps(p.to_dict(), indent=2)


@mcp.tool
async def link_goal(project_id: str, goal_id: str, unlink: bool = False) -> str:
    """Attach (or, with unlink=True, detach) a durable goal to/from a project. The
    link is by id only — the goal's status is joined live in list_projects /
    project_status, never copied. Idempotent."""
    try:
        p = (
            registry.unlink_goal(project_id, goal_id)
            if unlink
            else registry.link_goal(project_id, goal_id)
        )
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps(p.to_dict(), indent=2)


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
