"""DevClaw — MCP server.

Tools (every task/program submission is async — returns an id immediately and
runs in the background):
  - implement_feature / fix_bug / review_repository / onboard -> {task_id}
  - setup_cicd(workspace_dir)                                 -> {status, detail}  (sync)
  - start_program(workspace_dir, goal)              -> {program_id}  (planner decomposes into a task DAG)
  - get_status(task_id)            / list_tasks(status?, kind?, limit?)
  - get_program(program_id)        / list_programs(limit?)
  - get_events(program_id | task_id, since_id?, limit?)
  - cancel_task(task_id)           / cancel_program(program_id)  (deliberate abort)
  - register_project / list_projects / project_status / update_project / link_goal
        (the project registry — the control plane's source of truth for "which
         repos is devclaw working on + the live status of each"; also a CLI in
         devclaw/cli.py and a /projects dashboard view)

Transport:
  - DEVCLAW_TRANSPORT=stdio (default) — local dev + tests
  - DEVCLAW_TRANSPORT=http            — streamable-http on $DEVCLAW_PORT (default 8000);
                                        also serves /dashboard + /programs/:id/events (SSE)

State: SQLite at $DEVCLAW_DB (default ./devclaw.db).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__
from . import dashboard as _dash
from . import preview as _preview
from . import repo as _repo
from .goal_service import GoalService
from .project_registry import ProjectExists, ProjectRegistry, project_rollup
from .project_service import ProjectService
from .project_store import ProjectStore
from .state_store import StateStore
from .task_queue import TaskQueue

SERVER_NAME = "devclaw"
DB_PATH = os.path.abspath(os.environ.get("DEVCLAW_DB", "devclaw.db"))
HTTP_PORT = int(os.environ.get("DEVCLAW_PORT", "8000"))
# Default 0.0.0.0 so sibling compose containers (e.g. openclaw-gateway) can
# reach the endpoint. Set DEVCLAW_HOST=127.0.0.1 to restrict to loopback.
HTTP_HOST = os.environ.get("DEVCLAW_HOST", "0.0.0.0")
# Optional bearer-token guard for the HTTP transport. When DEVCLAW_TOKEN is set,
# every route except /health requires it — via `Authorization: Bearer <token>`
# (MCP clients) or a `?token=<token>` query param (the browser dashboard +
# EventSource, which can't set headers). Unset -> auth disabled (local dev).
AUTH_TOKEN = os.environ.get("DEVCLAW_TOKEN", "")
TOKEN_QS = f"?token={urllib.parse.quote(AUTH_TOKEN)}" if AUTH_TOKEN else ""

store = StateStore(DB_PATH)
_engine = os.environ.get("DEVCLAW_ENGINE", "")
if _engine == "stub":
    # Harness-validation mode: deterministic stub engine + cognition, no docker,
    # no claude. Proves the plumbing around the agent; never use in production.
    from .stub_engine import stub_engine, stub_goal_planner, stub_grill, stub_spec_planner

    sys.stderr.write(
        "⚠ DEVCLAW_ENGINE=stub — deterministic stub engine + cognition "
        "(NO OpenHands, NO claude). For harness validation only.\n"
    )
    queue = TaskQueue(store, planner=stub_goal_planner, runner=stub_engine)
    projects = ProjectService(ProjectStore(), queue, grill_caller=stub_grill, spec_planner=stub_spec_planner)
elif _engine == "host":
    # Real cognition + real OpenHands, but run on the HOST with NO sandbox.
    from .host_runner import run_host

    sys.stderr.write(
        "⚠ DEVCLAW_ENGINE=host — OpenHands runs on the HOST with NO sandbox "
        "isolation (agent has full filesystem access). Dev/validation only.\n"
    )
    queue = TaskQueue(store, runner=run_host)
    projects = ProjectService(ProjectStore(), queue)
else:
    queue = TaskQueue(store)
    projects = ProjectService(ProjectStore(), queue)
# The goal layer (folded-in goalclaw): durable, steerable, evaluated goals driven
# across heartbeats, dispatching into the SAME queue in-process. Owns goals under
# DEVCLAW_GOALS_DIR; the heartbeat + on-settle wake are started in the entrypoint.
goals = GoalService(queue, store)
# The project registry (control plane): the single source of truth for "which
# repos is devclaw working on, and what's the status of each". Thin — it links to
# goals by id and joins their live status on read (project_rollup), never caching
# phase. Shares the SQLite file with the state store.
registry = ProjectRegistry(DB_PATH)


def _goal_get(goal_id: str) -> dict:
    """Read-only goal status getter for the project rollup (raises KeyError)."""
    return goals.get_goal(goal_id)


mcp: FastMCP = FastMCP(SERVER_NAME, version=__version__)

LimitField = Field(ge=1, le=1000)


# ===== tools =================================================================


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


# ===== goal layer (durable, steerable, evaluated goals) ======================
# The folded-in goalclaw. A `program` is a bounded, one-shot DAG; a `goal` is an
# open-ended standing intent that DevClaw drives across many heartbeats —
# planning the next action, dispatching it into the queue, and EVALUATING whether
# the work is actually moving toward the objective (not just shipping PRs). These
# tools are the steer/observe surface: ask what's going on, correct it, force an
# evaluation.


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
    action; repo_url clones it if absent. verify_cmd: the gate (e.g. 'dotnet test')."""
    if not goal_id or not objective or not workspace_dir:
        raise ToolError("create_goal requires goal_id, objective, workspace_dir")
    try:
        return json.dumps(
            goals.create_goal(
                goal_id, objective=objective, workspace_dir=workspace_dir,
                done_when=done_when, backlog=backlog, cadence=cadence,
                repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
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
    action actually shipped: agent summary + gate verdict + PR), the
    investigating/grilling artifacts (discovery brief + agreed spec), and the tail
    of the LIVE event stream from whatever task is in flight — so you can see the
    engineer acting in near real time without SSHing to the box. Everything is
    bounded; call repeatedly to follow progress."""
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
    """Deliver an owner's reply to a goal that is waiting on them. This is the
    Telegram answer channel: when a goal is in its 'grilling' phase it asks scope
    questions one at a time; route the owner's reply here to answer the open
    question (the goal then asks the next one or finalizes the spec). When a goal
    is in 'plan_review', any reply here approves the plan and execution begins.
    The goal is poked to advance immediately. No-op (with an explanatory result)
    if the goal isn't currently awaiting input."""
    if not goal_id or not answer:
        raise ToolError("answer_goal requires goal_id and answer")
    try:
        return json.dumps(goals.answer_goal(goal_id, answer), indent=2)
    except KeyError:
        raise ToolError(f"unknown goal_id: {goal_id}")


@mcp.tool
async def evaluate_goal(goal_id: str) -> str:
    """Force a direction evaluation NOW and return the verdict + rationale. The
    evaluator judges the goal's actual delivered work against done_when (grounded
    in what shipped), not by counting backlog items. Any corrections it produces
    are fed back as steering. Use to ask 'is this going the right way?' on demand."""
    if not goal_id:
        raise ToolError("evaluate_goal requires goal_id")
    try:
        return json.dumps(await goals.evaluate_goal(goal_id), indent=2)
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


# ===== live preview hosting ==================================================
# Run a built app on the VPS and hand back clickable links (Swagger /docs +
# frontend). Long-lived, resource-capped containers; see devclaw/preview.py.


@mcp.tool
async def start_preview(workspace_dir: str, slug: str, port: int = 8000) -> str:
    """Run a project's BUILT app as a live preview on the VPS and return clickable
    URLs — the frontend and the API's Swagger /docs — so the owner can OPEN the
    thing, not just read the diff. Serves frontend + API on one origin/port (so a
    hard-coded localhost API base works behind a single SSH tunnel). Long-lived +
    resource-capped; replaces an existing preview for the same slug and evicts the
    oldest preview if at the VPS concurrency cap. workspace_dir = the goal's
    checkout (the built app); slug = a short stable name for the preview."""
    if not workspace_dir or not slug:
        raise ToolError("start_preview requires workspace_dir and slug")
    try:
        return json.dumps(await _preview.start_preview(workspace_dir, slug, port=port), indent=2)
    except _preview.PreviewError as err:
        raise ToolError(str(err))


@mcp.tool
async def preview_status(slug: str) -> str:
    """Status of a live preview: whether it exists, is running, is answering
    (ready), and its URLs. Use to check a preview started with start_preview."""
    if not slug:
        raise ToolError("preview_status requires slug")
    return json.dumps(await _preview.preview_status(slug), indent=2)


@mcp.tool
async def stop_preview(slug: str) -> str:
    """Stop and remove a live preview, freeing its VPS resources."""
    if not slug:
        raise ToolError("stop_preview requires slug")
    return json.dumps(await _preview.stop_preview(slug), indent=2)


@mcp.tool
async def list_previews() -> str:
    """List all live previews (running + stopped) with their status."""
    return json.dumps(await _preview.list_previews(), indent=2)


@mcp.tool
async def build_project(idea: str, workspace_dir: str) -> str:
    """Start a project from scratch. DevClaw GRILLS you — one question at a time,
    each with a recommended answer — to reach a shared understanding of what to
    build and how, before any code is written. Returns a project_id and the first
    question. Answer with answer_question(project_id, answer); repeat until
    status='ready' (a spec), then approve_spec(project_id) to build it (which may
    run for a long time via OpenHands)."""
    if not idea or not workspace_dir:
        raise ToolError("build_project requires idea and workspace_dir")
    return json.dumps(await projects.start(idea, workspace_dir), indent=2)


@mcp.tool
async def answer_question(project_id: str, answer: str) -> str:
    """Answer the project's current grill question. Returns the next question, or
    status='ready' with the finalized spec once the interview converges."""
    try:
        return json.dumps(await projects.answer(project_id, answer), indent=2)
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    except ValueError as err:
        raise ToolError(str(err))


@mcp.tool
async def get_project(project_id: str) -> str:
    """Status of a build-from-scratch project across all phases — idea,
    transcript, the outstanding question, the spec, and (once approved) the
    program_id of the running build."""
    project = projects.get(project_id)
    if not project:
        raise ToolError(f"unknown project_id: {project_id}")
    return json.dumps(project.to_dict(), indent=2)


@mcp.tool
async def steer(project_id: str, message: str) -> str:
    """Inject direction into a running build without stopping it. The message is
    folded into the project's not-yet-started tasks (work already running or done
    is untouched) and recorded in the project's steer log. Use to redirect
    mid-build — e.g. 'actually use Postgres, not SQLite'."""
    try:
        return json.dumps(await projects.steer(project_id, message), indent=2)
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")


@mcp.tool
async def approve_spec(project_id: str) -> str:
    """Approve a ready project spec and start building. Decomposes the spec into a
    milestone task DAG and hands it to the executor; returns the program_id. Poll
    get_program(program_id) or get_project(project_id) for progress."""
    try:
        return json.dumps(await projects.approve(project_id), indent=2)
    except KeyError:
        raise ToolError(f"unknown project_id: {project_id}")
    except ValueError as err:
        raise ToolError(str(err))


# ===== project registry (control plane) ======================================
# The portfolio view: which repos devclaw owns + the live status of each. Distinct
# from the build-from-scratch project tools above (build_project/get_project/…),
# which are the one-shot grill→spec→build interview. These manage the durable
# registry that links repos to their driving goals; status is joined live.


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
    plus the LIVE status of every goal driving it and a derived health signal.
    (The registry-level analogue of get_goal; get_project is the build-flow tool.)"""
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


# ===== dashboard + SSE (HTTP transport only) =================================
# Presentation lives in devclaw/dashboard.py (pure renderers); the routes here
# stay thin — fetch data, hand it to a renderer. _esc is re-exported for the few
# inline 404 strings + the SSE path.

_esc = _dash.esc


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse({"ok": True, "name": SERVER_NAME, "version": __version__})


@mcp.custom_route("/goals/answer", methods=["POST"])
async def goals_answer(request: Request) -> Response:
    """Deterministic reply→goal routing for the dedicated devclaw Telegram channel.
    The notify-relay bridge POSTs the owner's reply here; we route it to the single
    goal awaiting input (grilling answers the open question, plan_review approves).
    No agent, no inference — just the one waiting goal. Auth-guarded by the same
    bearer middleware as every other route (except /health)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "missing text"}, status_code=400)
    waiting = [g for g in goals.list_goals() if g.get("lifecycle") in ("grilling", "plan_review")]
    if not waiting:
        return JSONResponse({"routed_to": None, "reason": "no goal awaiting input"}, status_code=409)
    if len(waiting) > 1:
        return JSONResponse(
            {"routed_to": None, "reason": "multiple goals awaiting", "goals": [g["id"] for g in waiting]},
            status_code=409,
        )
    try:
        result = goals.answer_goal(waiting[0]["id"], text)
    except KeyError:
        return JSONResponse({"error": "goal vanished"}, status_code=409)
    return JSONResponse(result)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_index(_request: Request) -> Response:
    programs = store.list_programs(limit=50)
    return HTMLResponse(_dash.render_programs(programs, version=__version__, token_qs=TOKEN_QS))


@mcp.custom_route("/dashboard/{program_id}", methods=["GET"])
async def dashboard_program(request: Request) -> Response:
    program_id = request.path_params["program_id"]
    program = store.get_program(program_id)
    if not program:
        return HTMLResponse(_dash.render_not_found("program", program_id), status_code=404)
    return HTMLResponse(_dash.render_program(program, token_qs=TOKEN_QS))


@mcp.custom_route("/programs/{program_id}/events", methods=["GET"])
async def program_events(request: Request) -> Response:
    """Resumable SSE stream of one program's events.

    Resume protocol: the EventSource Last-Event-Id header (sent by the browser
    on auto-reconnect) is the cursor; each frame's id is the event row's PK.
    Live tail: SQLite has no LISTEN/NOTIFY, so we poll every 750ms after the
    initial backlog (cheap, indexed). Termination: when the program is terminal
    AND the last poll returned nothing new, emit a final `done` frame and close.
    """
    from sse_starlette.sse import EventSourceResponse  # local import: http-only dep path

    program_id = request.path_params["program_id"]
    if not store.get_program(program_id):
        return PlainTextResponse(f"unknown program: {program_id}", status_code=404)

    leh = request.headers.get("last-event-id")
    cursor = int(leh) if (leh and leh.isdigit() and int(leh) > 0) else 0

    async def gen():
        nonlocal cursor
        yield {"comment": "ok"}  # forces EventSource onopen even with zero events
        while True:
            if await request.is_disconnected():
                return
            try:
                drained = store.list_events(program_id=program_id, since_id=cursor, limit=200)
            except Exception as err:
                yield {"event": "error", "data": json.dumps({"message": str(err)})}
                return
            for ev in drained:
                yield {
                    "id": str(ev.id),
                    "data": json.dumps(
                        {
                            "id": ev.id,
                            "type": ev.type,
                            "source": ev.source,
                            "ts": ev.ts,
                            "payload": _safe_parse(ev.payload_json),
                        }
                    ),
                }
                cursor = ev.id
            current = store.get_program(program_id)
            terminal = current is not None and current.status in ("done", "failed")
            if terminal and not drained:
                yield {"event": "done", "data": json.dumps({"status": current.status})}
                return
            await asyncio.sleep(0.75)

    return EventSourceResponse(gen())


@mcp.custom_route("/goals", methods=["GET"])
async def dashboard_goals(_request: Request) -> Response:
    """Live overview of every durable goal — the 'what's devclaw doing' pane."""
    return HTMLResponse(_dash.render_goals(goals.list_goals(), version=__version__, token_qs=TOKEN_QS))


@mcp.custom_route("/projects", methods=["GET"])
async def dashboard_projects(_request: Request) -> Response:
    """Portfolio view — every registered project + its derived health, the
    control-plane overview that ties repos to the goals driving them."""
    items = [project_rollup(p, _goal_get) for p in registry.list()]
    return HTMLResponse(_dash.render_projects(items, version=__version__, token_qs=TOKEN_QS))


@mcp.custom_route("/goals/{goal_id}", methods=["GET"])
async def dashboard_goal(request: Request) -> Response:
    """Live detail for one goal: what it's working on NOW, what shipped, the log,
    and the live event tail. Reuses the same data as the tail_goal MCP tool."""
    goal_id = request.path_params["goal_id"]
    try:
        d = goals.tail_goal(goal_id, log_lines=40, deliveries_chars=8000, event_limit=40)
    except KeyError:
        return HTMLResponse(_dash.render_not_found("goal", goal_id), status_code=404)
    return HTMLResponse(_dash.render_goal(d, goal_id, token_qs=TOKEN_QS))


def _safe_parse(s: str) -> object:
    try:
        return json.loads(s)
    except Exception:
        return s


# ===== auth middleware =======================================================


class AuthMiddleware:
    """Pure-ASGI bearer-token gate. No-op when DEVCLAW_TOKEN is unset. /health
    stays open so container health checks don't need the token."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not AUTH_TOKEN or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        ok = auth == f"Bearer {AUTH_TOKEN}"
        if not ok:
            qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
            ok = qs.get("token", [None])[0] == AUTH_TOKEN
        if ok:
            await self.app(scope, receive, send)
            return
        resp = JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"www-authenticate": "Bearer"}
        )
        await resp(scope, receive, send)


# ===== entrypoint ============================================================


def _start_loops() -> None:
    """Start the two heartbeats: the task queue (resumes work + advances DAGs) and
    the goal layer (drives durable goals). Wire the queue's on-settle hook to the
    goal heartbeat so a finished task triggers an immediate goal tick in-process."""
    queue.start_ticking()
    queue.set_on_settle(goals.poke)
    goals.start()


async def _serve_stdio() -> None:
    _start_loops()
    await mcp.run_stdio_async()


async def _serve_http() -> None:
    import uvicorn
    from starlette.middleware import Middleware

    app = mcp.http_app(path="/mcp", middleware=[Middleware(AuthMiddleware)])
    _start_loops()
    config = uvicorn.Config(app, host=HTTP_HOST, port=HTTP_PORT, log_level="warning")
    await uvicorn.Server(config).serve()


def main() -> None:
    transport = os.environ.get("DEVCLAW_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        raise SystemExit(f'Unknown DEVCLAW_TRANSPORT={transport}; expected "stdio" or "http"')

    # Crash recovery before anything serves: reset tasks left 'running' by a
    # dead process so the heartbeat resumes them. Sync — runs before the loop.
    reaped = queue.recover()

    if transport == "stdio":
        sys.stderr.write(
            f"{SERVER_NAME} v{__version__} ready (stdio, db={DB_PATH}, recovered={reaped})\n"
        )
        asyncio.run(_serve_stdio())
    else:
        sys.stderr.write(
            f"{SERVER_NAME} v{__version__} ready "
            f"(http://{HTTP_HOST}:{HTTP_PORT}/mcp, db={DB_PATH}, recovered={reaped})\n"
        )
        asyncio.run(_serve_http())


if __name__ == "__main__":
    main()
