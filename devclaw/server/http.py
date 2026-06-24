"""HTTP custom routes — dashboard, SSE event stream, Telegram answer hook.

Presentation lives in ``devclaw.dashboard`` (pure renderers); the routes here
stay thin — fetch data, hand it to a renderer.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .. import __version__
from .. import dashboard as _dash
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
