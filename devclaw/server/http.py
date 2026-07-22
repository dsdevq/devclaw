"""HTTP custom routes — dashboard, SSE event stream, Telegram answer hook.

Presentation lives in ``devclaw.dashboard`` (pure renderers); the routes here
stay thin — fetch data, hand it to a renderer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import mimetypes
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .. import __version__
from .. import telemetry as _telemetry
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


@mcp.custom_route("/", methods=["GET"])
async def root_redirect(_request: Request) -> Response:
    """The human-facing surface is the console; a bare hostname visit should
    land there, not on a 404 (live-found 2026-07-09: the operator's bookmark
    pointed at `/`)."""
    return RedirectResponse(url="/console", status_code=307)


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


def _task_row(t) -> dict:
    """Wire shape for a Task row in the console — used by both ProjectDetail
    (loose tasks) and GoalDetail (dispatched tasks). Shape mirrors
    ``devclaw/server/console/src/api.ts`` ``TaskRow``."""
    return {
        "id": t.id,
        "kind": t.kind,
        "status": t.status,
        "goal": t.goal,
        "workspaceDir": t.workspace_dir,
        "parentGoalId": t.parent_goal_id,
        "createdAt": t.created_at,
        "completedAt": t.completed_at,
        "prUrl": t.pr_url,
    }


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


@mcp.custom_route("/goals/{goal_id}/resume", methods=["POST"])
async def goal_resume(request: Request) -> Response:
    """Console-facing Resume button — the recovery verb. Wraps
    goal_service.resume_goal: re-attempts the SAME contract on a blocked goal
    whose blocker was cleared out-of-band (no steering recorded, objective
    untouched). Idempotent — a no-op on a goal that isn't blocked. A goal blocked
    in FIRMING is refused by the service (answers must come through /answer)."""
    goal_id = request.path_params["goal_id"]
    try:
        result = goals.resume_goal(goal_id)
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": "cannot_resume", "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


@mcp.custom_route("/goals/{goal_id}/answer", methods=["POST"])
async def goal_answer(request: Request) -> Response:
    """Console-facing Answer button for a goal blocked in firming. Body is JSON
    `{"answers": {"<unknown_id>": "<answer>", ...}}` covering EVERY current
    unknown. Wraps goal_service.answer_unknowns (fires the next firming round).
    A partial/extra answer map, or a goal with no draft, returns 400 with the
    reason rather than a silent no-op."""
    goal_id = request.path_params["goal_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    answers = (body or {}).get("answers")
    if not isinstance(answers, dict) or not answers:
        return JSONResponse(
            {"error": "answers_required", "hint": "POST {\"answers\": {id: str}}"},
            status_code=400,
        )
    try:
        result = await goals.answer_unknowns(goal_id, answers)
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": "bad_answers", "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


# ── configuration surfaces ─────────────────────────────────────────────────
# A: a READ-ONLY catalog of the runtime env vars, parsed live from the enforced
#    single-source-of-truth doc (docs/reference/env-vars.md) so the table never
#    drifts. Secret values are masked — the value is never echoed to the browser.
# B: the EDITABLE per-project overrides (already DB-backed + live-resolved by the
#    registry; no restart needed). Global env stays read-only on purpose — those
#    are read at process start and can't be hot-edited, and a free-form env
#    editor would be a vector to inject a metered API key (the OAuth-only
#    invariant strips ANTHROPIC_* — never make them settable here).

def _resolve_env_doc() -> Path:
    """Locate docs/reference/env-vars.md across install layouts. Under an editable
    install / the source tree it sits at the repo root above this module; under a
    NON-editable install the package is copied to site-packages WITHOUT docs/, but
    the server runs with cwd at the repo root (/app in the container), so the
    cwd-relative candidate finds it. Falls back to the module-relative path (→ the
    catalog degrades to [] if the doc is genuinely absent)."""
    default = Path(__file__).resolve().parents[2] / "docs" / "reference" / "env-vars.md"
    for c in (default, Path.cwd() / "docs" / "reference" / "env-vars.md"):
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return default


_ENV_DOC = _resolve_env_doc()
_SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def _strip_md(s: str) -> str:
    return s.replace("`", "").replace("**", "").strip()


def _env_var_catalog() -> list[dict]:
    """Parse the env-var reference doc into rows the console renders: group, key,
    default, purpose, and the CURRENT value (masked for secrets). Best-effort —
    a missing/renamed doc degrades to [] rather than 500-ing the settings view."""
    try:
        text = _ENV_DOC.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    group = ""
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("## "):
            group = st[3:].strip()
            continue
        if not st.startswith("|") or "`" not in st:
            continue
        cells = [c.strip() for c in st.strip("|").split("|")]
        if len(cells) < 3:
            continue
        keycell = cells[0]
        if keycell.lower() == "var" or set(keycell) <= set("-: "):
            continue  # header / separator row
        key = keycell.split("`")[1].strip()  # first backticked token
        if not (key.isupper() and "_" in key and key.replace("_", "").isalnum()):
            continue
        default = _strip_md(cells[1])
        if default in ("—", "(unset)", "*(unset)*"):
            default = ""
        secret = any(h in key for h in _SECRET_HINTS)
        raw = os.environ.get(key, "")
        rows.append(
            {
                "group": group,
                "key": key,
                "default": default,
                "purpose": _strip_md("|".join(cells[2:])),
                "value": ("••••••" if raw else "") if secret else raw,
                "isSet": bool(raw),
                "secret": secret,
            }
        )
    return rows


# ---- Evals projection (ADR 0006) ------------------------------------------
# Read-only JSON over the eval_outcomes projection + the night_reports table.
# Un-prefixed `.json` data-endpoint convention (like /config/env.json,
# /projects.json, /control.json) — NOT the contract's illustrative
# `/api/evals/...`. The console Evals tab reads these.

_EVALS_JSON_DEFAULT_LIMIT = 100
_EVALS_JSON_MAX_LIMIT = 1000


def _evals_limit(request: Request) -> tuple[int, Response | None]:
    try:
        limit = int(request.query_params.get("limit", _EVALS_JSON_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return 0, JSONResponse({"error": "bad_limit"}, status_code=400)
    if limit <= 0:
        return 0, JSONResponse({"error": "bad_limit"}, status_code=400)
    return min(limit, _EVALS_JSON_MAX_LIMIT), None


@mcp.custom_route("/evals/outcomes.json", methods=["GET"])
async def evals_outcomes_json(request: Request) -> Response:
    """Recent ``eval_outcomes`` projection rows (ADR 0006), newest settle first.
    Params: ``limit`` (default 100, max 1000), ``source`` (``live``|``basket``).
    Read-only; delegates the SELECT to the store (PR1's read method)."""
    limit, err = _evals_limit(request)
    if err is not None:
        return err
    source = request.query_params.get("source") or None
    if source not in (None, "live", "basket"):
        return JSONResponse({"error": "bad_source"}, status_code=400)
    return JSONResponse(store.list_eval_outcomes(source=source, limit=limit))


@mcp.custom_route("/evals/nights.json", methods=["GET"])
async def evals_nights_json(request: Request) -> Response:
    """Recent ``night_reports`` rows (ADR 0006), newest window first. Param:
    ``limit`` (default 100, max 1000). Defensive: if the table doesn't exist
    yet (the night-report tranche unmerged) the store returns [], never a 500."""
    limit, err = _evals_limit(request)
    if err is not None:
        return err
    return JSONResponse(store.list_night_reports(limit=limit))


@mcp.custom_route("/config/env.json", methods=["GET"])
async def config_env_json(_request: Request) -> Response:
    """Read-only catalog of every runtime env var + its current value (secrets
    masked). Editing global env needs a container restart, so this view is
    deliberately read-only; the editable knobs live per-project (below)."""
    return JSONResponse({"vars": _env_var_catalog()})


#: per-project override fields the console may edit, with their validators.
_OVR_BOOL = ("automerge", "autodeploy", "review_gate", "verify_done")
_OVR_STR = {"merge_strategy": ("squash", "merge", "rebase"),
            "browser_gate_mode": ("flexible", "strict")}
#: free-form string overrides — validated by shape, not enum. sandbox_image is
#: a docker image ref (ADR 0005's escape hatch); the shared grammar (defined
#: at the registry write choke point, which also enforces it as the backstop)
#: blocks flag-shaped/whitespace junk here with a friendly 400.
from ..project_registry import _IMAGE_REF_RE as _OVR_IMAGE_RE  # noqa: E402
_OVR_FREE_STR = ("sandbox_image",)


def _project_overrides(p) -> dict:
    return {
        "automerge": p.automerge,
        "autodeploy": p.autodeploy,
        "review_gate": p.review_gate,
        "verify_done": p.verify_done,
        "merge_strategy": p.merge_strategy,
        "browser_gate_mode": p.browser_gate_mode,
        "sandbox_image": p.sandbox_image,
    }


@mcp.custom_route("/projects/{project_id}/config.json", methods=["GET"])
async def project_config_get(request: Request) -> Response:
    """A project's editable overrides. `null` = inherit the devclaw-wide default;
    a value = pinned for this repo. Resolution is live (registry read per call)."""
    pid = request.path_params["project_id"]
    p = registry.get(pid)
    if p is None:
        return JSONResponse({"error": "not_found", "id": pid}, status_code=404)
    return JSONResponse({"overrides": _project_overrides(p)})


@mcp.custom_route("/projects/{project_id}/config", methods=["POST"])
async def project_config_set(request: Request) -> Response:
    """Update a project's overrides. Body `{field: value|null}` — only listed
    fields change (`null` clears back to the default). Unknown fields or bad
    values are rejected 400; secrets/infra env are NOT reachable here by design."""
    pid = request.path_params["project_id"]
    if registry.get(pid) is None:
        return JSONResponse({"error": "not_found", "id": pid}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict) or not body:
        return JSONResponse({"error": "empty_patch"}, status_code=400)
    patch: dict = {}
    for k, v in body.items():
        if k in _OVR_BOOL:
            if v is not None and not isinstance(v, bool):
                return JSONResponse({"error": "bad_value", "field": k, "hint": "bool|null"}, status_code=400)
            patch[k] = v
        elif k in _OVR_STR:
            if v is not None and v not in _OVR_STR[k]:
                return JSONResponse({"error": "bad_value", "field": k, "hint": f"one of {_OVR_STR[k]}|null"}, status_code=400)
            patch[k] = v
        elif k in _OVR_FREE_STR:
            if v is not None and (not isinstance(v, str) or not _OVR_IMAGE_RE.fullmatch(v)):
                return JSONResponse({"error": "bad_value", "field": k, "hint": "docker image ref|null"}, status_code=400)
            patch[k] = v
        else:
            return JSONResponse({"error": "unknown_field", "field": k}, status_code=400)
    registry.update(pid, **patch)
    return JSONResponse({"overrides": _project_overrides(registry.get(pid))})


@mcp.custom_route("/control.json", methods=["GET"])
async def control_json(request: Request) -> Response:
    """Dispatch-control state for the console: the manual operator hold, the daily
    run-window, and the (automatic) quota pause — plus whether NEW dispatch is
    blocked right now and why. Read by the console's Dispatch panel."""
    from ..dispatch_gate import operator_block
    from ..state_store import _now_ms

    now = _now_ms()
    on, hold_reason = store.operator_hold()
    schedule = store.get_run_schedule()
    q_until, q_reason = store.global_pause()
    quota_active = q_until > now
    op_blocked, op_reason = operator_block((on, hold_reason), schedule, now)
    blocked = op_blocked or quota_active
    reason = op_reason if op_blocked else (f"quota: {q_reason}" if quota_active else "")
    return JSONResponse({
        "operatorHold": {"on": on, "reason": hold_reason},
        "schedule": schedule,
        "goalSchedules": store.list_goal_schedules(),
        "quotaPause": {"activeUntilMs": q_until if quota_active else 0, "reason": q_reason},
        "blocked": blocked,
        "reason": reason,
    })


@mcp.custom_route("/control/pause", methods=["POST"])
async def control_pause(request: Request) -> Response:
    """Turn on the manual operator hold — stops all NEW dispatch (in-flight tasks
    finish). Optional JSON body ``{"reason": "..."}``. Idempotent."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str((body or {}).get("reason") or "").strip()
    store.set_operator_hold(True, reason)
    on, r = store.operator_hold()
    return JSONResponse({"operatorHold": {"on": on, "reason": r}})


@mcp.custom_route("/control/resume", methods=["POST"])
async def control_resume(request: Request) -> Response:
    """Clear the manual operator hold. Does NOT touch an active quota pause or the
    run-window — those gate independently, so dispatch resumes only if nothing
    else is holding it."""
    store.set_operator_hold(False)
    return JSONResponse({"operatorHold": {"on": False, "reason": ""}})


async def _apply_schedule(request: Request, goal_id: "str | None") -> Response:
    """Validate a schedule body and persist it (global when ``goal_id`` is None,
    else that goal's own window). Shared by the global and per-goal routes so the
    same fail-closed validation guards both — a typo must 400, never silently
    disable the window (the gate fails open)."""
    from zoneinfo import ZoneInfo

    from ..dispatch_gate import _parse_hhmm

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    b = body or {}
    cur = store.get_run_schedule(goal_id)
    enabled = bool(b.get("enabled", cur["enabled"]))
    start = str(b.get("start") or cur["start"])
    end = str(b.get("end") or cur["end"])
    tz = str(b.get("tz") or cur["tz"])
    if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
        return JSONResponse(
            {"error": "bad_time", "hint": "start/end must be HH:MM"}, status_code=400
        )
    try:
        ZoneInfo(tz)
    except Exception:
        return JSONResponse(
            {"error": "bad_tz", "hint": "IANA name, e.g. Europe/Kyiv"}, status_code=400
        )
    store.set_run_schedule(enabled, start, end, tz, goal_id=goal_id)
    return JSONResponse({"schedule": store.get_run_schedule(goal_id)})


@mcp.custom_route("/control/schedule", methods=["POST"])
async def control_schedule(request: Request) -> Response:
    """Set the engine-wide daily run-window. Body:
    ``{"enabled": bool, "start": "HH:MM", "end": "HH:MM", "tz": "Area/City"}``.
    Missing fields keep their current value. A bad time or timezone is rejected
    (400) rather than silently accepted — the gate fails open, so a typo here
    would quietly disable the window."""
    return await _apply_schedule(request, None)


@mcp.custom_route("/goals/{goal_id}/schedule", methods=["GET"])
async def goal_schedule_get(request: Request) -> Response:
    """This goal's OWN run-window (a night/off-hours narrowing on top of the
    engine-wide window). A disabled default means the goal follows only the
    global window."""
    goal_id = request.path_params["goal_id"]
    return JSONResponse({"goalId": goal_id, "schedule": store.get_run_schedule(goal_id)})


@mcp.custom_route("/goals/{goal_id}/schedule", methods=["POST"])
async def goal_schedule_set(request: Request) -> Response:
    """Set THIS goal's own daily run-window — same body + validation as the global
    route. Confines a token-heavy standing goal to off-hours without gating the
    rest of the engine. Send ``{"enabled": false}`` to stop it restricting."""
    goal_id = request.path_params["goal_id"]
    return await _apply_schedule(request, goal_id)


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
    # Dispatch cap = the runaway backstop the goal tick already enforces
    # (max(len(backlog)+2, len(checklist)+2) — see goal/tick.py:1028). Surface
    # it so the console can show "N / cap" and, when phase=blocked, the banner
    # can render "N of N dispatched — merge to unblock".
    backlog_len = len(g.get("backlog") or [])
    base_cap = backlog_len + 2
    dispatch_cap: int
    try:
        # Console DISPLAY path: read the checklist off the GOAL store (the
        # module-level `store` is the SQLite StateStore and has no checklist
        # — the old call here always raised AttributeError into the fallback).
        # on_corrupt="none": a torn checklist must degrade the cap readout,
        # never 500 the dashboard — the tick blocks the goal loudly instead.
        checklist = goals._goal_store.read_checklist(goal_id, on_corrupt="none")
        cap_c = (len(checklist.items) + 2) if checklist else base_cap
        dispatch_cap = max(base_cap, cap_c)
    except Exception:
        dispatch_cap = base_cap
    # Dispatched tasks — every Task the goal heartbeat filed against this goal
    # (parent_goal_id match). Includes both live and terminal tasks; the
    # console renders them as a timeline of what the goal actually dispatched.
    # One fetch serves both the task timeline (newest 50) and the usage sum
    # below (all 500 — a goal past the dispatch cap rarely nears that, and a
    # 50-row sum would quietly understate the "what did this goal cost"
    # number the block exists to answer).
    task_rows = store.list_tasks(parent_goal_id=goal_id, limit=500)
    dispatched_tasks = [_task_row(t) for t in task_rows[:50]]
    # Firming unknowns — only meaningful (and only read) when the goal is blocked
    # awaiting owner answers. Best-effort: a torn/absent draft degrades to [] so
    # the console shows a plain Resume rather than 500-ing the detail view.
    unknowns: list[dict] = []
    if phase == "blocked":
        try:
            draft = goals._goal_store.read_firmed_draft(goal_id)
            if draft is not None:
                unknowns = [
                    {
                        "id": u.id,
                        "question": u.question,
                        "why": getattr(u, "why", ""),
                        # The firming model already emits structured options per
                        # unknown; carrying them through lets the console render
                        # one-tap choices instead of a bare textarea.
                        "options": list(getattr(u, "options", []) or []),
                        "defaultIfNoAnswer": getattr(u, "default_if_no_answer", None),
                    }
                    for u in draft.unknowns
                ]
        except Exception:
            unknowns = []
    # Usage rollup — cognition from the goal's trace totals, worker from the
    # per-task "usage" blocks the runner records into result_json. Pure reads;
    # best-effort: a torn trace/row degrades to null, never 500s the view.
    usage: dict | None = None
    try:
        totals = store.trace_totals(goal_id=goal_id)
        worker = _telemetry.sum_task_usage(t.result_json for t in task_rows)
        usage = {
            "cognitionTokensIn": totals["cognition_tokens_in"],
            "cognitionTokensOut": totals["cognition_tokens_out"],
            "cognitionCostUsd": totals["cognition_cost_usd"],
            "workerInputTokens": worker["input_tokens"],
            "workerOutputTokens": worker["output_tokens"],
            "workerCostUsd": worker["cost_usd"],
            "tasksWithUsage": worker["tasks_with_usage"],
            "totalTokens": (
                totals["cognition_tokens_in"] + totals["cognition_tokens_out"]
                + worker["input_tokens"] + worker["output_tokens"]
            ),
            "totalCostUsd": round(totals["cognition_cost_usd"] + worker["cost_usd"], 6),
        }
    except Exception:
        usage = None
    return JSONResponse(
        {
            "id": g["id"],
            "objective": g.get("objective") or "",
            "phase": phase,
            "phaseLabel": _phase_label(phase),
            "lifecycle": g.get("lifecycle"),
            "direction": g.get("direction"),
            "actionsDispatched": g.get("actions_dispatched", 0),
            "dispatchCap": dispatch_cap,
            "inFlight": g.get("in_flight"),
            "timeline": timeline,
            "blockedOn": g.get("blocked_on"),
            "blockedKind": g.get("blocked_kind", ""),
            "unknowns": unknowns,
            "usage": usage,
            "tasks": dispatched_tasks,
        }
    )


_GH_PR_URL_RE = __import__("re").compile(
    r"^https?://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)/?$"
)


def _parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """Return (owner, repo, number) or None. Rejects non-github.com URLs — the
    merge endpoint uses this as its allow-check so a spoofed pr_url can't
    trick us into shelling `gh` at an arbitrary host/repo."""
    if not isinstance(url, str):
        return None
    m = _GH_PR_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def _collect_goal_pr_rows(goal_id: str) -> list[dict]:
    """Read delivery traces for this goal, extract PRs, dedupe by URL — the
    LAST delivery for a given PR wins so if a mission re-mentions a PR (e.g.
    on a retry) the newer action_label surfaces. Merge/close-state enrichment
    happens in the endpoint (per-row `gh pr view` probe); this step only
    reads local state.

    Dedup relies on ``read_traces`` returning ascending id order — trace ids
    are monotonic, so we don't need to compare wall-clock ts (which can tie
    inside the same millisecond)."""
    seen: dict[str, dict] = {}
    for ev in store.read_traces(goal_id=goal_id, kind="delivery", limit=1000):
        payload = ev.get("payload") or {}
        pr_url = str(payload.get("pr_url") or "").strip()
        if not pr_url:
            continue
        parsed = _parse_pr_url(pr_url)
        if parsed is None:
            continue
        owner, repo, number = parsed
        seen[pr_url] = {
            "prUrl": pr_url,
            "prNumber": number,
            "repo": f"{owner}/{repo}",
            "actionLabel": str(payload.get("action_label") or ""),
            "gatePassed": payload.get("gate_passed"),
            "ts": ev.get("ts") or "",
            "_id": ev.get("id") or 0,
        }
    rows = list(seen.values())
    rows.sort(key=lambda r: r.get("_id") or 0, reverse=True)
    for r in rows:
        r.pop("_id", None)
    return rows


async def _probe_pr_state(repo: str, number: int) -> dict:
    """Live-fetch PR state via `gh pr view`. Failures degrade to unknown state
    so a network hiccup or a deleted branch never blocks the whole page."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "view", str(number),
        "--repo", repo,
        "--json", "state,mergeable,mergeStateStatus,title,mergedAt",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=12.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"state": "UNKNOWN", "mergeable": "UNKNOWN", "error": "timeout"}
    if proc.returncode != 0:
        return {
            "state": "UNKNOWN",
            "mergeable": "UNKNOWN",
            "error": (stderr.decode("utf-8", "replace") or "gh failed").strip()[:200],
        }
    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"state": "UNKNOWN", "mergeable": "UNKNOWN", "error": "parse"}


@mcp.custom_route("/goals/{goal_id}/prs.json", methods=["GET"])
async def goal_prs_json(request: Request) -> Response:
    """PRs opened by this goal, with live GitHub state so the console can show
    the correct Merge button per row without another round-trip.

    Sources: `traces(kind='delivery')` rows carry `pr_url` — parsed and deduped
    locally. Each surviving row is enriched with a live `gh pr view` probe
    (state/mergeable/title/mergedAt) so `state==OPEN and mergeable==MERGEABLE`
    is the exact condition the Merge button enables on. Probes run in parallel
    to keep page-load reasonable when a mission has many PRs open. Traces are
    the intentional source over `deliveries.md`: structured payload, not
    markdown extraction; and stays consistent with the SSE feed."""
    goal_id = request.path_params["goal_id"]
    try:
        goals.get_goal(goal_id)
    except KeyError:
        return JSONResponse({"error": "not_found", "id": goal_id}, status_code=404)

    rows = _collect_goal_pr_rows(goal_id)
    if not rows:
        return JSONResponse({"prs": []})

    states = await asyncio.gather(
        *[_probe_pr_state(r["repo"], r["prNumber"]) for r in rows]
    )
    for row, state in zip(rows, states):
        row["state"] = state.get("state") or "UNKNOWN"
        row["mergeable"] = state.get("mergeable") or "UNKNOWN"
        row["mergeStateStatus"] = state.get("mergeStateStatus") or None
        row["title"] = state.get("title") or ""
        row["mergedAt"] = state.get("mergedAt") or None
        if state.get("error"):
            row["error"] = state["error"]
    return JSONResponse({"prs": rows})


@mcp.custom_route("/prs/merge", methods=["POST"])
async def pr_merge(request: Request) -> Response:
    """Console-facing merge button. Body: `{"prUrl": "https://github.com/…"}`.

    Guarded by `_parse_pr_url`: only URLs matching a canonical github.com PR
    path are accepted, so a spoofed body can't turn this into an arbitrary
    shell. Squash + delete-branch matches the merge policy we already use for
    the closeloop mission chain — one-shot slice per PR."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    pr_url = (body or {}).get("prUrl")
    parsed = _parse_pr_url(pr_url if isinstance(pr_url, str) else "")
    if parsed is None:
        return JSONResponse(
            {"error": "invalid_pr_url", "hint": "expected https://github.com/<owner>/<repo>/pull/<n>"},
            status_code=400,
        )
    owner, repo, number = parsed
    slug = f"{owner}/{repo}"
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "merge", str(number),
        "--repo", slug,
        "--squash",
        "--delete-branch",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return JSONResponse(
            {"merged": False, "error": "timeout"}, status_code=504
        )
    if proc.returncode != 0:
        err = (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace")).strip()
        return JSONResponse(
            {"merged": False, "error": err[:400] or "gh pr merge failed"},
            status_code=502,
        )
    return JSONResponse(
        {"merged": True, "prUrl": pr_url, "output": stdout.decode("utf-8", "replace").strip()[:200]}
    )


_TRACES_JSON_DEFAULT_LIMIT = 200
_TRACES_JSON_MAX_LIMIT = 1000


@mcp.custom_route("/traces.json", methods=["GET"])
async def traces_json(request: Request) -> Response:
    """General telemetry read over the ``traces`` table — the same filters the
    ``devclaw trace list`` CLI exposes, for dashboards/scripts that already
    speak HTTP to this server.

    Query params: ``goal`` (or ``goal_id``), ``kind``, ``role`` (cognition
    payload field), ``since`` (30m/24h/7d or ISO timestamp), ``errors_only``
    (1/true), ``limit`` (default 200, max 1000). Rows come back newest-first.
    Every filter is applied in SQL by ``StateStore.read_traces`` — the
    production table holds 200k+ rows, so this route never loads-then-filters
    in Python. Read-only: auth/token handling is the transport-wide middleware,
    same as every other route here."""
    q = request.query_params
    since_ms = None
    since = q.get("since")
    if since:
        try:
            since_ms = _telemetry.parse_since(since)
        except ValueError as exc:
            return JSONResponse({"error": "bad_since", "detail": str(exc)}, status_code=400)
    try:
        limit = int(q.get("limit", _TRACES_JSON_DEFAULT_LIMIT))
    except ValueError:
        return JSONResponse({"error": "bad_limit"}, status_code=400)
    if limit <= 0:
        return JSONResponse({"error": "bad_limit"}, status_code=400)
    limit = min(limit, _TRACES_JSON_MAX_LIMIT)
    rows = store.read_traces(
        goal_id=q.get("goal") or q.get("goal_id") or None,
        kind=q.get("kind") or None,
        role=q.get("role") or None,
        since_ms=since_ms,
        errors_only=str(q.get("errors_only", "")).lower() in ("1", "true", "yes"),
        limit=limit,
        newest_first=True,
    )
    return JSONResponse({"traces": rows, "count": len(rows), "limit": limit})


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
    # Recent standalone tasks in this project's workspace — the "loose" ones
    # not owned by any goal (dispatch_task calls). Tasks owned by a goal show
    # up inside that goal's Dispatched Tasks section, not here, so users don't
    # see double-counts. See ~/memory/projects/devclaw/plan.md "The noun model".
    loose_tasks: list[dict] = []
    if p.workspace_dir:
        for t in store.list_tasks(
            workspace_dir=p.workspace_dir,
            parent_goal_id_is_null=True,
            limit=25,
        ):
            loose_tasks.append(_task_row(t))
    # Warn-first one-goal-per-project (2026-07-04): if >1 active goal is
    # joined to this project, surface a banner. Under the standing rule a
    # project pursues one goal at a time — cancel + refile instead of stacking.
    warnings: list[dict] = []
    if len(active) > 1:
        warnings.append(
            {
                "code": "multiple_active_goals",
                "message": (
                    f"This project has {len(active)} active goals. Under the "
                    "one-goal-per-project rule a project pursues one goal at "
                    "a time — cancel the extras or refile."
                ),
                "goalIds": [row["id"] for row in active],
            }
        )
    return JSONResponse(
        {
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "repoUrl": p.repo_url,
            "previewUrl": p.preview_url,
            "active": active,
            "archived": archived,
            "tasks": loose_tasks,
            "warnings": warnings,
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
