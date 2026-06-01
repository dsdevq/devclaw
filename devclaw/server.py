"""DevClaw — MCP server.

Tools (every task/program submission is async — returns an id immediately and
runs in the background):
  - implement_feature / fix_bug / review_repository -> {task_id}
  - start_program(workspace_dir, goal)              -> {program_id}  (planner decomposes into a task DAG)
  - get_status(task_id)            / list_tasks(status?, kind?, limit?)
  - get_program(program_id)        / list_programs(limit?)
  - get_events(program_id | task_id, since_id?, limit?)
  - cancel_task(task_id)           / cancel_program(program_id)  (deliberate abort)

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
import sys
import urllib.parse
from html import escape as _html_escape
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__
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
mcp: FastMCP = FastMCP(SERVER_NAME, version=__version__)

LimitField = Field(ge=1, le=1000)


# ===== tools =================================================================


@mcp.tool
async def implement_feature(
    workspace_dir: str, goal: str, notify_url: Optional[str] = None
) -> str:
    """Submit a natural-language coding goal to be executed by OpenHands in the
    given workspace_dir. Returns a task_id immediately; the task runs
    asynchronously. Poll get_status(task_id), or pass notify_url to be pushed
    the result. Use for new features / open-ended changes; prefer fix_bug for an
    existing defect, and review_repository for a read-only review."""
    if not workspace_dir or not goal:
        raise ToolError("implement_feature requires workspace_dir and goal")
    task_id = queue.submit(
        kind="implement_feature", workspace_dir=workspace_dir, goal=goal, notify_url=notify_url
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, indent=2)


@mcp.tool
async def fix_bug(
    workspace_dir: str, description: str, notify_url: Optional[str] = None
) -> str:
    """Submit a bug-fix task. Like implement_feature, but with a prompt that
    biases OpenHands toward reading existing code first, making the smallest
    fix, not refactoring unrelated code, and running the tests. Returns task_id
    immediately. Same optional notify_url as implement_feature."""
    if not workspace_dir or not description:
        raise ToolError("fix_bug requires workspace_dir and description")
    task_id = queue.submit(
        kind="fix_bug", workspace_dir=workspace_dir, goal=description, notify_url=notify_url
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
    kind: Optional[Literal["implement_feature", "fix_bug", "review_repository"]] = None,
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


# ===== build a project from scratch ==========================================


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


# ===== dashboard + SSE (HTTP transport only) =================================


def _esc(s: str) -> str:
    return _html_escape(s, quote=True)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse({"ok": True, "name": SERVER_NAME, "version": __version__})


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_index(_request: Request) -> Response:
    programs = store.list_programs(limit=50)
    rows = "".join(
        (
            "<tr>"
            f'<td><a href="/dashboard/{p.id}{TOKEN_QS}">{p.id[:8]}</a></td>'
            f"<td>{_esc(p.status)}</td>"
            f"<td>{_esc(_iso(p.created_at))}</td>"
            f"<td>{_esc(p.goal[:117] + '...' if len(p.goal) > 120 else p.goal)}</td>"
            "</tr>"
        )
        for p in programs
    )
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>devclaw — programs</title>
<style>
 body{{font:14px/1.4 -apple-system,system-ui,sans-serif;margin:2rem;color:#eee;background:#0d1117}}
 a{{color:#7ab8ff}}
 table{{border-collapse:collapse;width:100%;margin-top:1rem}}
 th,td{{padding:.4rem .6rem;border-bottom:1px solid #30363d;text-align:left}}
 th{{background:#161b22}}
</style></head><body>
<h1>devclaw programs <small>v{_esc(__version__)}</small></h1>
<p>{len(programs)} program(s). Click a row to open the live event stream.</p>
<table><thead><tr><th>id</th><th>status</th><th>created</th><th>goal</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""
    return HTMLResponse(html)


@mcp.custom_route("/dashboard/{program_id}", methods=["GET"])
async def dashboard_program(request: Request) -> Response:
    program_id = request.path_params["program_id"]
    program = store.get_program(program_id)
    if not program:
        return HTMLResponse(f"<p>unknown program: {_esc(program_id)}</p>", status_code=404)
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>devclaw — {_esc(program_id)}</title>
<style>
 body{{font:13px/1.4 -apple-system,system-ui,sans-serif;margin:2rem;color:#eee;background:#0d1117}}
 a{{color:#7ab8ff}}
 h1{{font-size:1.2rem}}
 #events{{margin-top:1rem;border:1px solid #30363d;border-radius:6px;padding:1rem;background:#161b22;max-height:80vh;overflow:auto;font-family:ui-monospace,monospace;font-size:12px}}
 .ev{{padding:.2rem 0;border-bottom:1px solid #21262d}}
 .type{{color:#79c0ff;font-weight:bold}} .source{{color:#8b949e}} .id{{color:#6e7681}}
</style></head><body>
<p><a href="/dashboard{TOKEN_QS}">&larr; all programs</a></p>
<h1>program {_esc(program_id)} <small>({_esc(program.status)})</small></h1>
<p>{_esc(program.goal)}</p>
<div id="events"></div>
<script>
 const box = document.getElementById('events');
 const src = new EventSource('/programs/{program_id}/events{TOKEN_QS}');
 src.onmessage = (e) => {{
   try {{
     const ev = JSON.parse(e.data);
     const div = document.createElement('div');
     div.className = 'ev';
     div.innerHTML = '<span class=id>#' + ev.id + '</span> ' +
                     '<span class=type>' + ev.type + '</span> ' +
                     '<span class=source>(' + ev.source + ')</span>';
     box.appendChild(div);
     box.scrollTop = box.scrollHeight;
   }} catch (err) {{ /* swallow */ }}
 }};
 src.onerror = () => {{ /* browser auto-reconnects with Last-Event-Id */ }};
</script>
</body></html>"""
    return HTMLResponse(html)


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


def _iso(ms: int) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


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


async def _serve_stdio() -> None:
    queue.start_ticking()  # heartbeat: resumes recovered work + advances DAGs
    await mcp.run_stdio_async()


async def _serve_http() -> None:
    import uvicorn
    from starlette.middleware import Middleware

    app = mcp.http_app(path="/mcp", middleware=[Middleware(AuthMiddleware)])
    queue.start_ticking()
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
