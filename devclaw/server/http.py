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
    all_goals = goals.list_goals()
    items = [project_rollup(p, all_goals) for p in registry.list()]
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


# Fixed left-to-right order the Goal Detail phase-timeline renders. Keep in
# sync with `phaseNames` in the Claude Design mock (Goal Detail.dc.html:373).
_TIMELINE_PHASES = ["investigating", "firming", "executing", "verifying", "done"]


def _phase_index(current: str | None) -> int:
    """Where along the timeline the goal is right now. Non-timeline phases
    (idle, in_flight, blocked, cancelled, error) collapse to 'executing' —
    they all represent forward-of-firming work in the current lifecycle."""
    if current is None:
        return 0
    if current in _TIMELINE_PHASES:
        return _TIMELINE_PHASES.index(current)
    return _TIMELINE_PHASES.index("executing")


# Design taxonomy from Goal Detail.dc.html: cognition/subprocess/dispatch/
# delivery/notify. Real backend event types are runner-specific and irregular,
# so we normalize here with a best-effort mapper. PR#7 will tighten this by
# stamping the kind at emit time.
_KIND_EXACT = {
    "cancelled": "notify",
    "reaped": "notify",
    "workspace_break_tripped": "notify",
    "StdoutLine": "subprocess",
    "StderrLine": "subprocess",
    "StubBuildEvent": "dispatch",
}


def _event_kind(event_type: str) -> str:
    if event_type in _KIND_EXACT:
        return _KIND_EXACT[event_type]
    t = event_type.lower()
    if any(k in t for k in ("message", "llm", "think", "plan", "cognition")):
        return "cognition"
    if any(k in t for k in ("stdout", "stderr", "cmd", "shell", "bash", "exec")):
        return "subprocess"
    if any(k in t for k in ("action", "tool", "dispatch")):
        return "dispatch"
    if any(k in t for k in ("delivery", "merge", "commit", "pull_request", " pr ", "pr_")):
        return "delivery"
    return "notify"


def _project_event_row(ev, *, kind: str, payload: object) -> dict:
    """Frame shape the console's Goal Detail feed reads. Kept flat so the
    React side can render without another normalization pass."""
    return {
        "id": ev.id,
        "kind": kind,
        "type": ev.type,
        "source": ev.source,
        "ts": ev.ts,
        "payload": payload,
    }


@mcp.custom_route("/goals/{goal_id}/cancel", methods=["POST"])
async def goal_cancel(request: Request) -> Response:
    """Console-facing cancel button. Wraps goal_service.cancel_goal — same
    entrypoint the MCP tool uses, so behavior (terminal-phase no-op, in-flight
    teardown) is identical whether the caller is Claude or the browser."""
    goal_id = request.path_params["goal_id"]
    try:
        result = goals.cancel_goal(goal_id)
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)
    return JSONResponse(result)


@mcp.custom_route("/goals/{goal_id}/steer", methods=["POST"])
async def goal_steer(request: Request) -> Response:
    """Console-facing steer button. Body is JSON `{"message": "..."}`.

    Steering is additive — appends to the goal's inbox and pokes the loop
    (goal_service.steer_goal), so it can flip a blocked goal back to idle.
    Empty or missing message returns 400 rather than a silent no-op."""
    goal_id = request.path_params["goal_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    message = (body or {}).get("message")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse(
            {"error": "message_required", "hint": "POST {\"message\": str}"},
            status_code=400,
        )
    try:
        result = goals.steer_goal(goal_id, message.strip())
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)
    return JSONResponse(result)


@mcp.custom_route("/goals/{goal_id}/events", methods=["GET"])
async def goal_events(request: Request) -> Response:
    """SSE stream of events for the goal's CURRENT in_flight task/program.

    Contract: the stream is keyed to the ref that was in_flight at connect
    time. When the goal moves off that ref (new task, or no in_flight), we
    emit a `done` frame; the client reconnects to pick up the new ref.
    Resume: EventSource sends `last-event-id` on auto-reconnect; we use it as
    the SQLite events.id cursor (same pattern as the existing programs SSE)."""
    from sse_starlette.sse import EventSourceResponse  # local import: http-only

    goal_id = request.path_params["goal_id"]
    try:
        g = goals.get_goal(goal_id)
    except KeyError:
        return PlainTextResponse(f"unknown goal: {goal_id}", status_code=404)

    in_flight = g.get("in_flight")
    if not in_flight:
        # No live task — return an empty stream that immediately closes with a
        # `done` frame. The client can reconnect once phase/in_flight change.
        async def empty_gen():
            yield {"comment": "no in_flight"}
            yield {"event": "done", "data": json.dumps({"reason": "no_in_flight"})}

        return EventSourceResponse(empty_gen())

    # Pin the ref at connect time. list_events wants program_id OR task_id.
    ref_kind = in_flight.get("ref_kind") or ("task" if in_flight.get("id") else "program")
    ref_id = in_flight.get("id")
    list_kwargs = (
        {"task_id": ref_id} if ref_kind == "task" else {"program_id": ref_id}
    )

    leh = request.headers.get("last-event-id")
    cursor = int(leh) if (leh and leh.isdigit() and int(leh) > 0) else 0

    async def gen():
        nonlocal cursor
        yield {"comment": "ok"}
        while True:
            if await request.is_disconnected():
                return
            try:
                drained = store.list_events(since_id=cursor, limit=200, **list_kwargs)
            except Exception as err:
                yield {"event": "error", "data": json.dumps({"message": str(err)})}
                return
            for ev in drained:
                payload = _safe_parse(ev.payload_json)
                yield {
                    "id": str(ev.id),
                    "data": json.dumps(
                        _project_event_row(
                            ev, kind=_event_kind(ev.type), payload=payload
                        )
                    ),
                }
                cursor = ev.id
            # Re-check the goal's in_flight — if it changed under us, close so
            # the client reconnects and re-pins.
            try:
                current = goals.get_goal(goal_id)
            except KeyError:
                yield {"event": "done", "data": json.dumps({"reason": "goal_gone"})}
                return
            current_ref = (current.get("in_flight") or {}).get("id")
            if current_ref != ref_id:
                yield {
                    "event": "done",
                    "data": json.dumps({"reason": "in_flight_rotated"}),
                }
                return
            await asyncio.sleep(0.75)

    return EventSourceResponse(gen())


@mcp.custom_route("/goals/{goal_id}.json", methods=["GET"])
async def goal_json(request: Request) -> Response:
    """Goal Detail feed — header, objective, phase-timeline shape, pills.

    Reuses goal_service.get_goal so the observe surface stays a single source
    of truth. Timeline node timestamps arrive in PR#7 (phase_history)."""
    goal_id = request.path_params["goal_id"]
    try:
        g = goals.get_goal(goal_id)
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)
    phase = g.get("phase")
    current_index = _phase_index(phase)
    # Timeline slots are the fixed 5-slot design contract. For each slot, if the
    # goal's phase_history recorded arriving at that phase, we stamp the FIRST
    # arrival. Repeated visits (idle → executing → idle → executing) don't
    # rewrite the label — matches the design's "when did this phase happen"
    # semantic, not "most recent".
    history_first_at: dict[str, str] = {}
    for entry in g.get("phase_history") or []:
        pn = str(entry.get("phase") or "")
        if pn and pn not in history_first_at and entry.get("at"):
            history_first_at[pn] = str(entry["at"])

    def _iso_to_ms(iso: str) -> int | None:
        try:
            ts = _dt.datetime.fromisoformat(iso)
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return int(ts.timestamp() * 1000)

    timeline = []
    for i, name in enumerate(_TIMELINE_PHASES):
        stamp_iso = history_first_at.get(name)
        timeline.append(
            {
                "name": name,
                "reached": i <= current_index,
                "current": i == current_index,
                "timestampMs": _iso_to_ms(stamp_iso) if stamp_iso else None,
            }
        )
    return JSONResponse(
        {
            "id": g["id"],
            "objective": g.get("objective") or "",
            "phase": phase,
            "phaseLabel": _phase_label(phase),
            "lifecycle": g.get("lifecycle"),
            "direction": g.get("direction"),
            "actionsDispatched": g.get("actions_dispatched", 0),
            "inFlight": g.get("in_flight"),
            "timeline": timeline,
            "blockedOn": g.get("blocked_on"),
        }
    )


@mcp.custom_route("/projects/{project_id}.json", methods=["GET"])
async def project_json(request: Request) -> Response:
    """Project Detail feed — header (name, repo, preview) + active/archived goal
    rows. Same phase/direction source as get_goal so any drift on the goal side
    reflects here without extra plumbing."""
    project_id = request.path_params["project_id"]
    p = registry.get(project_id)
    if p is None:
        return JSONResponse({"error": "not_found", "id": project_id}, status_code=404)
    # Discover this project's goals by workspace_dir match — same join rule
    # as project_rollup. `goal_ids` on the Project row is advisory only and
    # can go stale (see project_registry_link_stale memory + docstring).
    from ..project_registry import _normalize_workspace

    proj_ws = _normalize_workspace(p.workspace_dir)
    matching_ids: list[str] = []
    if proj_ws is not None:
        for g in goals.list_goals():
            if _normalize_workspace(g.get("workspace_dir")) == proj_ws:
                matching_ids.append(g["id"])
    active: list[dict] = []
    archived: list[dict] = []
    for gid in matching_ids:
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
    all_goals = goals.list_goals()
    for p in registry.list():
        rollup = project_rollup(p, all_goals)
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "activeGoals": _active_goal_count(rollup["goals"]),
                "lastActivityMs": _last_activity_ms(rollup["goals"]),
                "repoUrl": p.repo_url or None,
                "previewUrl": p.preview_url or None,
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
