"""HTTP custom routes — dashboard, SSE event stream, Telegram answer hook.

Presentation lives in ``devclaw.dashboard`` (pure renderers); the routes here
stay thin — fetch data, hand it to a renderer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import mimetypes
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from .. import __version__
from . import dashboard as _dash
from ..project_registry import project_rollup
from ._state import (
    SERVER_NAME,
    TOKEN_QS,
    _goal_get,
    goals,
    mcp,
    registry,
    store,
)

_esc = _dash.esc


def _safe_parse(s: str) -> object:
    try:
        return json.loads(s)
    except Exception:
        return s


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse({"ok": True, "name": SERVER_NAME, "version": __version__})


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


# ---- Console (Vite + React SPA, served as a static bundle) ----------------
# The three-screen web console lives under `devclaw/server/console/`. `npm run
# build` writes `console/dist/`; the bytes on disk are what these routes serve.
# The SPA does client-side routing under basename="/console", so any path that
# doesn't map to a file falls through to `index.html`.

_CONSOLE_DIST = Path(__file__).resolve().parent / "console" / "dist"


def _serve_console_file(rel: str) -> Response:
    if not _CONSOLE_DIST.exists():
        return PlainTextResponse(
            "devclaw console bundle not built — run `npm --prefix "
            "devclaw/server/console run build`",
            status_code=503,
        )
    # Resolve safely inside dist. `Path.resolve()` normalizes `..`, then we
    # verify the resolved path stays inside the dist tree.
    target = (_CONSOLE_DIST / rel).resolve()
    try:
        target.relative_to(_CONSOLE_DIST)
    except ValueError:
        return PlainTextResponse("forbidden", status_code=403)
    if target.is_file():
        media, _ = mimetypes.guess_type(str(target))
        return FileResponse(str(target), media_type=media)
    # SPA fallback: unknown paths serve the app shell so client-side routing works.
    index = _CONSOLE_DIST / "index.html"
    if not index.is_file():
        return PlainTextResponse("console index.html missing from bundle", status_code=500)
    return FileResponse(str(index), media_type="text/html")


@mcp.custom_route("/console", methods=["GET"])
async def console_index(_request: Request) -> Response:
    return _serve_console_file("index.html")


@mcp.custom_route("/console/{path:path}", methods=["GET"])
async def console_asset(request: Request) -> Response:
    return _serve_console_file(request.path_params["path"] or "index.html")


# ---- JSON API surfaces the console reads ----------------------------------


def _last_activity_ms(goals_list: list[dict]) -> int | None:
    """Newest `progress.last_at` (ISO ts) across a project's linked goals,
    converted to epoch ms. `None` when no goal has fired progress yet.

    Kept here (not on Project) so the registry stays free of goal-shape
    knowledge — reading live phase/progress is the rollup's job."""
    best: int | None = None
    for g in goals_list:
        if g.get("missing"):
            continue
        last_at = (g.get("progress") or {}).get("last_at")
        if not isinstance(last_at, str):
            continue
        try:
            ts = _dt.datetime.fromisoformat(last_at)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        ms = int(ts.timestamp() * 1000)
        if best is None or ms > best:
            best = ms
    return best


def _active_goal_count(goals_list: list[dict]) -> int:
    """A goal is 'active' from the console's POV when it isn't terminal — the
    Projects Home column matches the design's semantics ('Active goals')."""
    terminal = {"done", "cancelled", "error", "achieved"}
    return sum(
        1
        for g in goals_list
        if not g.get("missing") and (g.get("phase") not in terminal)
    )


_TERMINAL_PHASES = {"done", "cancelled", "error", "achieved"}


def _phase_label(phase: str | None) -> str:
    """Map internal phase to the design's label vocabulary. `done` is presented
    as `Achieved` per the mock (Project Detail archived section).
    """
    if phase is None:
        return "—"
    return {"done": "Achieved"}.get(phase, phase.capitalize())


def _goal_action_label(goal_id: str) -> str:
    """One-line 'what's this goal currently doing' — the design's In-flight
    action column. Terminal goals fall back to their last direction note; active
    goals surface the human `next` hint, then the in_flight tool. Returns '—'
    when nothing useful is known."""
    try:
        g = _goal_get(goal_id)
    except KeyError:
        return "—"
    phase = g.get("phase")
    if phase in _TERMINAL_PHASES:
        direction = g.get("direction") or {}
        note = direction.get("note") or ""
        return note.strip() or "—"
    nxt = (g.get("next") or "").strip()
    if nxt:
        return nxt
    in_flight = g.get("in_flight") or {}
    tool = in_flight.get("tool")
    return tool if tool else "—"


def _goal_last_update_ms(goal_id: str) -> int | None:
    try:
        g = _goal_get(goal_id)
    except KeyError:
        return None
    last_at = (g.get("progress") or {}).get("last_at")
    if not isinstance(last_at, str):
        return None
    try:
        ts = _dt.datetime.fromisoformat(last_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return int(ts.timestamp() * 1000)


def _goal_row(goal_id: str) -> dict:
    try:
        g = _goal_get(goal_id)
    except KeyError:
        return {
            "id": goal_id,
            "phase": None,
            "phaseLabel": "Missing",
            "action": "—",
            "lastUpdateMs": None,
        }
    phase = g.get("phase")
    return {
        "id": goal_id,
        "phase": phase,
        "phaseLabel": _phase_label(phase),
        "action": _goal_action_label(goal_id),
        "lastUpdateMs": _goal_last_update_ms(goal_id),
    }


@mcp.custom_route("/projects/{project_id}.json", methods=["GET"])
async def project_json(request: Request) -> Response:
    """Project Detail feed — header (name, repo, preview) + active/archived goal
    rows. Same phase/direction source as get_goal so any drift on the goal side
    reflects here without extra plumbing."""
    project_id = request.path_params["project_id"]
    p = registry.get(project_id)
    if p is None:
        return JSONResponse({"error": "not_found", "id": project_id}, status_code=404)
    active: list[dict] = []
    archived: list[dict] = []
    for gid in p.goal_ids:
        row = _goal_row(gid)
        (archived if row["phase"] in _TERMINAL_PHASES else active).append(row)
    active.sort(key=lambda r: r.get("lastUpdateMs") or 0, reverse=True)
    archived.sort(key=lambda r: r.get("lastUpdateMs") or 0, reverse=True)
    return JSONResponse(
        {
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "repoUrl": p.repo_url,
            "previewUrl": p.preview_url,
            "active": active,
            "archived": archived,
        }
    )


@mcp.custom_route("/projects.json", methods=["GET"])
async def projects_json(_request: Request) -> Response:
    """Projects Home feed: name, status, active goal count, last activity.

    Same source of truth as the `/projects` HTML route — project_rollup — so
    the two views can't drift. Shape is documented in
    `devclaw/server/console/src/api.ts` (ProjectRow)."""
    out: list[dict] = []
    for p in registry.list():
        rollup = project_rollup(p, _goal_get)
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "activeGoals": _active_goal_count(rollup["goals"]),
                "lastActivityMs": _last_activity_ms(rollup["goals"]),
            }
        )
    return JSONResponse(out)


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
