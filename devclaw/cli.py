"""devclaw CLI — drive the project registry from a terminal.

The third face of the control plane: chat (MCP tools) and the dashboard (HTTP)
already exist; this is the CLI. It talks to the SAME stores the server uses —
the registry's SQLite table (``DEVCLAW_DB``) and the durable goals
(``DEVCLAW_GOALS_DIR``) — directly and read-mostly, so it works without the
server running and never needs the queue/engine spun up.

One family of subcommands is an EXCEPTION to "read-mostly against the stores":
``cognition decompose|breakdown`` is a **no-side-effects planning dry-run**. It
makes real cognition calls (the same OAuth ``claude --print`` path the
heartbeat uses, via :func:`devclaw.planner.call_claude`) to show how a goal
decomposes into a milestone checklist — the ONE planning spine both durable
goals and programs ride (ADR 0003) — WITHOUT dispatching anything: zero
docker, zero task-queue, zero state mutation. It never touches the registry or
GoalStore — an operator uses it to inspect planning in isolation from
execution, at the cost of the model call(s).

Usage:
  python -m devclaw.cli cognition decompose "<objective>" --done-when "<text>"
                                            [--repo DIR] [-v] [--json]
  python -m devclaw.cli trace list [--goal G] [--kind K] [--role R] [--since 24h|<iso>]
                                   [--errors-only] [--limit N] [--json]
  python -m devclaw.cli trace report [--since 24h|<iso>] [--json]
  python -m devclaw.cli projects list [--status active|paused|archived] [--json]
  python -m devclaw.cli projects show <id> [--json]
  python -m devclaw.cli projects register <id> <name> [--repo-url U] [--workspace-dir D]
                                                       [--preview-url U] [--notes N]
  python -m devclaw.cli projects update <id> [--name ...] [--repo-url ...] [--status ...] ...
  python -m devclaw.cli projects link <id> <goal_id> [--unlink]
  python -m devclaw.cli projects archive <id>
  python -m devclaw.cli projects rm <id>

Output is human-readable by default; pass ``--json`` to list/show for the raw
rollup (the same shape the MCP tools return), so the CLI is scriptable too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Awaitable, Callable, Optional

from .goal.store import GoalStore
from .project_registry import ProjectExists, ProjectRegistry, project_rollup
from .state_store import StateStore
from .telemetry import (
    compute_scorecard,
    compute_trace_report,
    format_scorecard,
    format_trace_report,
    parse_since,
)

# Snapshot collector for the `cognition` dry-run's optional --repo grounding.
# Imported as a module global (not called via task_git.) so tests patch it on
# THIS namespace — same convention as planner.py / task_queue's git wrappers
# (see .claude/rules/cognition-prompts.md). Best-effort, never raises.
from .task_git import _review_repo_context_sync  # noqa: F401


def _db_path() -> str:
    return os.path.abspath(os.environ.get("DEVCLAW_DB", "devclaw.db"))


def _goals_dir() -> str:
    return os.path.expanduser(os.environ.get("DEVCLAW_GOALS_DIR", "~/memory/goals"))


def _list_goals(goal_store: GoalStore) -> list[dict]:
    """CLI-side mirror of goal_service.list_goals — reads straight from
    GoalStore so the CLI works without the queue/engine. Shape includes
    workspace_dir so project_rollup can do the workspace-match join."""
    out: list[dict] = []
    for gid in goal_store.list_goal_ids():
        g = goal_store.load_goal(gid)
        s = goal_store.load_status(gid)
        out.append({
            "id": gid,
            "workspace_dir": g.workspace_dir,
            "phase": s.phase,
            "lifecycle": s.lifecycle or "executing",
            "blocked_on": s.blocked_on,
            "progress": {"last_at": s.last_progress_at, "stalled": s.no_progress_notified},
            "direction": (
                {"verdict": s.last_eval_verdict, "at": s.last_eval_at, "note": s.last_eval_note}
                if s.last_eval_verdict else None
            ),
        })
    return out


# ---- rendering -------------------------------------------------------------


def _fmt_project_line(p: dict) -> str:
    health = p.get("health", "?")
    ngoals = len(p.get("goals", []))
    preview = p.get("previewUrl") or "—"
    return (
        f"{p['id']:<28} {health:<9} {p['status']:<9} "
        f"goals={ngoals:<3} preview={preview}"
    )


def _print_show(p: dict) -> None:
    print(f"{p['id']}  —  {p['name']}")
    print(f"  health:    {p.get('health')}")
    print(f"  status:    {p['status']}")
    print(f"  repo:      {p.get('repoUrl') or '—'}")
    print(f"  workspace: {p.get('workspaceDir') or '—'}")
    print(f"  preview:   {p.get('previewUrl') or '—'}")
    def _ovr(val, on="on", off="off") -> str:
        return "inherit (devclaw default)" if val is None else (on if val else off)

    print(f"  automerge: {_ovr(p.get('automerge'))}")
    ms = p.get("mergeStrategy")
    print(f"  merge-strategy: {ms if ms is not None else 'inherit (devclaw default)'}")
    print(f"  autodeploy: {_ovr(p.get('autodeploy'))}")
    print(f"  review-gate: {_ovr(p.get('reviewGate'))}")
    print(f"  verify-done: {_ovr(p.get('verifyDone'))}")
    bgm = p.get("browserGateMode")
    print(f"  browser-gate-mode: {bgm if bgm is not None else 'inherit (devclaw default)'}")
    if p.get("notes"):
        print(f"  notes:     {p['notes']}")
    goals = p.get("goals", [])
    print(f"  goals ({len(goals)}):")
    for g in goals:
        if g.get("missing"):
            print(f"    - {g['id']}  [MISSING — dangling link]")
            continue
        direction = g.get("direction")
        verdict = f" · {direction['verdict']}" if direction else ""
        stalled = " · STALLED" if (g.get("progress") or {}).get("stalled") else ""
        blocked = f" · blocked: {g['blocked_on']}" if g.get("blocked_on") else ""
        print(f"    - {g['id']}  [{g.get('phase')}/{g.get('lifecycle')}]{verdict}{stalled}{blocked}")


# ---- commands --------------------------------------------------------------


def _cmd_list(reg: ProjectRegistry, all_goals, args) -> int:
    items = [project_rollup(p, all_goals) for p in reg.list(status=args.status)]
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("no projects registered")
        return 0
    for p in items:
        print(_fmt_project_line(p))
    return 0


def _cmd_show(reg: ProjectRegistry, all_goals, args) -> int:
    p = reg.get(args.id)
    if p is None:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    rolled = project_rollup(p, all_goals)
    if args.json:
        print(json.dumps(rolled, indent=2))
    else:
        _print_show(rolled)
    return 0


def _cmd_register(reg: ProjectRegistry, all_goals, args) -> int:
    try:
        _onoff = {"on": True, "off": False}
        p = reg.create(
            id=args.id, name=args.name, repo_url=args.repo_url,
            workspace_dir=args.workspace_dir, preview_url=args.preview_url,
            notes=args.notes or "",
            automerge=(None if args.automerge is None else args.automerge == "on"),
            merge_strategy=args.merge_strategy,
            autodeploy=(None if args.autodeploy is None else _onoff[args.autodeploy]),
            review_gate=(None if args.review_gate is None else _onoff[args.review_gate]),
            verify_done=(None if args.verify_done is None else _onoff[args.verify_done]),
            browser_gate_mode=args.browser_gate_mode,
        )
    except ProjectExists:
        print(f"project already exists: {args.id}", file=sys.stderr)
        return 1
    print(f"registered {p.id}")
    return 0


def _cmd_update(reg: ProjectRegistry, all_goals, args) -> int:
    override_kwargs: dict = {}
    _onoff = {"on": True, "off": False, "inherit": None}
    for field, val in (("automerge", args.automerge), ("autodeploy", args.autodeploy),
                       ("review_gate", args.review_gate), ("verify_done", args.verify_done)):
        if val is not None:
            override_kwargs[field] = _onoff[val]
    if args.merge_strategy is not None:
        override_kwargs["merge_strategy"] = None if args.merge_strategy == "inherit" else args.merge_strategy
    if args.browser_gate_mode is not None:
        override_kwargs["browser_gate_mode"] = None if args.browser_gate_mode == "inherit" else args.browser_gate_mode
    try:
        reg.update(
            args.id, name=args.name, repo_url=args.repo_url,
            workspace_dir=args.workspace_dir, preview_url=args.preview_url,
            status=args.status, notes=args.notes,
            **override_kwargs,
        )
    except KeyError:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    print(f"updated {args.id}")
    return 0


def _cmd_link(reg: ProjectRegistry, all_goals, args) -> int:
    try:
        if args.unlink:
            reg.unlink_goal(args.id, args.goal_id)
            print(f"unlinked {args.goal_id} from {args.id}")
        else:
            reg.link_goal(args.id, args.goal_id)
            print(f"linked {args.goal_id} to {args.id}")
    except KeyError:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    return 0


def _cmd_archive(reg: ProjectRegistry, all_goals, args) -> int:
    try:
        reg.update(args.id, status="archived")
    except KeyError:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    print(f"archived {args.id}")
    return 0


def _cmd_rm(reg: ProjectRegistry, all_goals, args) -> int:
    if reg.delete(args.id):
        print(f"removed {args.id}")
        return 0
    print(f"unknown project: {args.id}", file=sys.stderr)
    return 1


def _cmd_scorecard(args) -> int:
    """Print the L8 scorecard (merge rate, verdict distribution, steer rate,
    first-pass hit rate) rolled up over the last ``--window-hours`` (default
    168 = one week). Reads state_store directly, no engine/server needed."""
    store = StateStore(_db_path())
    try:
        sc = compute_scorecard(store, window_hours=int(args.window_hours))
    finally:
        store.close()
    if args.json:
        print(json.dumps(sc, indent=2))
    else:
        print(format_scorecard(sc))
    return 0


def _trace_ts_iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ms or 0) / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _fmt_trace_event(row: dict) -> str:
    """One trace event → one greppable line. Kind-specific detail mirrors
    ``Tracer.render_timeline`` but stays single-line for terminal scanning."""
    p = row.get("payload") or {}
    kind = row.get("kind", "?")
    goal = row.get("goal_id") or "-"
    if kind == "cognition":
        err = p.get("error") or ""
        detail = f"{p.get('role', '?')} ({p.get('model', '?')}, {p.get('latency_ms', 0)}ms)"
        if err:
            detail += f" ERROR: {err}"
    elif kind == "tick":
        detail = f"lifecycle={p.get('lifecycle', '')} phase={p.get('phase', '')} -> {p.get('outcome', '')}"
    elif kind == "dispatch":
        detail = f"{p.get('tool', '')} ref={p.get('ref_id', '')} engine={p.get('engine', '') or '-'}"
    elif kind == "subprocess":
        err = p.get("error") or ""
        detail = f"{p.get('cmd', '')} ({p.get('latency_ms', 0)}ms, exit={p.get('exit_code')})"
        if err:
            detail += f" ERROR: {err}"
    elif kind == "delivery":
        gp = p.get("gate_passed")
        gate = "pass" if gp is True else ("FAIL" if gp is False else "-")
        detail = f"gate={gate} {p.get('pr_url') or ''} {p.get('action_label', '')}".rstrip()
    elif kind == "notify":
        detail = f"[{p.get('level', '')}] {str(p.get('text', ''))[:120]}"
    elif kind == "trend_check":
        detail = (
            f"{p.get('signal', '')} ({p.get('scope', '')}) "
            f"{'FIRED' if p.get('fired') else p.get('reason', '')}"
        )
    else:
        detail = json.dumps({k: v for k, v in p.items() if k != "kind"}, default=str)[:120]
    return f"{_trace_ts_iso(row.get('ts', 0))}  #{row.get('id', '?'):<7} {kind:<11} goal={goal:<24} {detail}"


def _cmd_trace_list(args) -> int:
    """List trace events — the general telemetry read the owner used to
    hand-write sqlite for. All filtering happens in SQL inside read_traces;
    output is chronological (we fetch the newest N, then reverse)."""
    since_ms = None
    if args.since:
        try:
            since_ms = parse_since(args.since)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    store = StateStore(_db_path())
    try:
        rows = store.read_traces(
            goal_id=args.goal,
            kind=args.kind,
            role=args.role,
            since_ms=since_ms,
            errors_only=args.errors_only,
            limit=int(args.limit),
            newest_first=True,
        )
    finally:
        store.close()
    rows.reverse()  # fetch newest N, print oldest-first for reading order
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no trace events match")
        return 0
    for r in rows:
        print(_fmt_trace_event(r))
    return 0


def _cmd_trace_report(args) -> int:
    """Deterministic day-report over tasks + traces. Pure SQL aggregation —
    NO LLM anywhere on this path."""
    try:
        since_ms = parse_since(args.since)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store = StateStore(_db_path())
    try:
        rep = compute_trace_report(store, since_ms=since_ms)
    finally:
        store.close()
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(format_trace_report(rep))
    return 0


def _fmt_schedule(s: dict) -> str:
    state = "enabled" if s.get("enabled") else "disabled"
    return f"{state}  {s.get('start')}–{s.get('end')} {s.get('tz')}"


def _cmd_schedule_show(args) -> int:
    """Show the engine-wide run-window and, without ``--goal``, every per-goal
    window; with ``--goal G`` just that goal's own window."""
    store = StateStore(_db_path())
    try:
        if args.goal:
            s = store.get_run_schedule(args.goal)
            if args.json:
                print(json.dumps({"goal": args.goal, "schedule": s}, indent=2))
            else:
                print(f"{args.goal}: {_fmt_schedule(s)}")
            return 0
        glob = store.get_run_schedule()
        per_goal = store.list_goal_schedules()
        if args.json:
            print(json.dumps({"global": glob, "goals": per_goal}, indent=2))
            return 0
        print(f"global: {_fmt_schedule(glob)}")
        if per_goal:
            print("per-goal:")
            for gid, s in sorted(per_goal.items()):
                print(f"  {gid:<28} {_fmt_schedule(s)}")
        else:
            print("per-goal: (none)")
        return 0
    finally:
        store.close()


def _cmd_schedule_set(args) -> int:
    """Set the engine-wide window, or a single goal's own window with ``--goal``.
    Rejects a bad time/timezone (the gate fails open, so a silent typo would
    quietly disable the window)."""
    from zoneinfo import ZoneInfo

    from .dispatch_gate import _parse_hhmm

    store = StateStore(_db_path())
    try:
        cur = store.get_run_schedule(args.goal)
        enabled = cur["enabled"]
        if args.enable:
            enabled = True
        elif args.disable:
            enabled = False
        start = args.start or cur["start"]
        end = args.end or cur["end"]
        tz = args.tz or cur["tz"]
        if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
            print("bad time: start/end must be HH:MM", file=sys.stderr)
            return 1
        try:
            ZoneInfo(tz)
        except Exception:
            print(f"bad timezone: {tz} (use an IANA name, e.g. Europe/Kyiv)", file=sys.stderr)
            return 1
        store.set_run_schedule(enabled, start, end, tz, goal_id=args.goal)
        who = args.goal or "global"
        print(f"{who}: {_fmt_schedule(store.get_run_schedule(args.goal))}")
        return 0
    finally:
        store.close()


def _cmd_schedule_clear(args) -> int:
    """Remove a window so it stops restricting dispatch (a cleared per-goal window
    falls back to the global window only)."""
    store = StateStore(_db_path())
    try:
        store.clear_run_schedule(args.goal)
        print(f"cleared {args.goal or 'global'} run-window")
        return 0
    finally:
        store.close()


# ---- cognition dry-run (planner/decomposer, no execution) ------------------
#
# Inspection-only: ONE real cognition call, NO docker / queue / state / heartbeat.
# The caller factories below are module globals so tests inject a fake claude
# caller by monkeypatching them — the same seam decompose already
# expose via their `claude_caller=` parameters, surfaced at the CLI edge.


def _default_decomposer_caller() -> Callable[[str], Awaitable[str]]:
    """Production decomposer-tier caller (opus, role='goal_decomposer', larger
    timeout). Reuses the decomposer module's own factory — no duplicate wiring."""
    from .goal.decomposer import default_caller

    return default_caller()


def _default_grill_caller() -> Callable[[str], Awaitable[str]]:
    """Production scope-grill caller (sonnet, role='grill'). The waiter's
    interview step, reused for the `breakdown` dry-run's finalize turn. A module
    global so tests patch it here (same seam as the planner/decomposer callers)."""
    from .elicitation import default_caller

    return default_caller()


def _default_firming_caller() -> Callable[[str], Awaitable[str]]:
    """Production firming caller (opus, role='goal_firming'). Structurally
    completes the goal (derives done_when + firmed extras) between grill and
    decompose. A module global so tests patch it here."""
    from .goal.phases.firming import default_caller

    return default_caller()


async def _repo_context(workspace_dir: str) -> str:
    """Async wrapper around the best-effort workspace snapshot (the #227
    collector). Runs the blocking git probe in a thread; degrades to '' on any
    hiccup — grounding is optional, it never fails the dry-run. Looks up
    :func:`_review_repo_context_sync` as a module global so tests patch it here."""
    return await asyncio.to_thread(_review_repo_context_sync, workspace_dir)


async def _grounded_context(repo: Optional[str]) -> Optional[str]:
    """Collect REPOSITORY CONTEXT when --repo is given; None otherwise (an
    ungrounded plan). Best-effort: a snapshot failure degrades to None, never
    aborts the dry-run."""
    if not repo:
        return None
    try:
        ctx = await _repo_context(os.path.abspath(os.path.expanduser(repo)))
    except Exception as exc:  # noqa: BLE001 — best-effort grounding
        print(f"note: repo snapshot failed ({exc}); planning ungrounded", file=sys.stderr)
        return None
    return ctx or None


async def _traced(producer: Awaitable) -> tuple[object, int, dict, int]:
    """Await ``producer`` (a coroutine that makes exactly ONE cognition call)
    under an in-memory tracer so we can read back real latency + token usage
    (the CLI attaches no persistent tracer, so this is the only place the
    envelope's usage is observable). Returns ``(result, latency_ms, usage,
    cognition_calls)``; ``cognition_calls`` lets the caller assert the one-call
    contract."""
    from .loom.trace import Tracer, tracer_scope

    tracer = Tracer(label="cli-cognition")
    started = time.monotonic()
    with tracer_scope(tracer):
        result = await producer
    measured_ms = int((time.monotonic() - started) * 1000)
    events = tracer.by_kind("cognition")
    ev = events[-1] if events else {}
    latency_ms = int(ev.get("latency_ms") or 0) or measured_ms
    usage = {
        "tokens_in": ev.get("tokens_in"),
        "tokens_out": ev.get("tokens_out"),
        "cost_usd": ev.get("cost_usd"),
    }
    return result, latency_ms, usage, len(events)


def _fmt_usage(latency_ms: int, usage: dict) -> str:
    parts = [f"{latency_ms} ms"]
    ti, to, cost = usage.get("tokens_in"), usage.get("tokens_out"), usage.get("cost_usd")
    if ti is not None or to is not None:
        parts.append(
            f"tokens in={ti if ti is not None else '?'} "
            f"out={to if to is not None else '?'}"
        )
    if cost is not None:
        parts.append(f"${cost:.4f}")
    return "   ".join(parts)


def _render_checklist(checklist) -> list[str]:
    lines: list[str] = []
    # Group by milestone, preserving first-seen order; None → "(no milestone)".
    order: list[Optional[str]] = []
    groups: dict[Optional[str], list] = {}
    for item in checklist.items:
        key = item.milestone
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    for ms in order:
        lines.append(f"milestone: {ms}" if ms else "milestone: (none)")
        for it in groups[ms]:
            dep = f"  ← depends_on: {', '.join(it.depends_on)}" if it.depends_on else ""
            scaffold = "  [scaffold]" if it.scaffold else ""
            lines.append(f"  ● {it.id}  [{it.status}]{scaffold}{dep}")
            lines.append(f"      {it.requirement}")
            lines.append(f"      evidence_target: {it.evidence_target}")
            if it.addresses_files:
                lines.append(f"      files: {', '.join(it.addresses_files)}")
            if it.note:
                lines.append(f"      note: {it.note}")
            lines.append("")
    if checklist.open_questions:
        lines.append("open_questions:")
        for q in checklist.open_questions:
            lines.append(f"  - {q}")
        lines.append("")
    if checklist.notes:
        lines.append("notes:")
        for n in checklist.notes:
            lines.append(f"  - {n}")
        lines.append("")
    return lines


def _cmd_cognition_decompose(args) -> int:
    """Dry-run the goal decomposer: ONE real cognition call → a rendered
    milestone checklist. Same no-side-effects contract as `plan`."""
    from .goal.decomposer import GoalDecomposerError, build_prompt, decompose
    from .goal.models import Goal

    caller = _default_decomposer_caller()
    workspace_dir = os.path.abspath(os.path.expanduser(args.repo)) if args.repo else os.getcwd()
    # A throwaway in-memory Goal — the decomposer only reads its facts; nothing
    # is persisted (no GoalStore is ever constructed on this path).
    goal_obj = Goal(
        id="cli-dryrun",
        objective=args.objective,
        cadence="1d",
        engine="devclaw",
        workspace_dir=workspace_dir,
        done_when=args.done_when,
    )

    async def _run() -> tuple[str, object, int, dict, int]:
        repo_context = await _grounded_context(args.repo)
        # build_prompt is called once here only to DISPLAY the exact prompt for
        # -v; decompose() builds the byte-identical prompt internally and makes
        # the single cognition call (the caller runs exactly once inside it).
        prompt = build_prompt(goal_obj, repo_context=repo_context)
        checklist, latency_ms, usage, ncalls = await _traced(
            decompose(goal_obj, claude_caller=caller, repo_context=repo_context)
        )
        return prompt, checklist, latency_ms, usage, ncalls

    try:
        prompt, checklist, latency_ms, usage, ncalls = asyncio.run(_run())
    except GoalDecomposerError as exc:
        print(f"decomposer failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface the parse/schema failure
        print(f"decomposer output unusable: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            {
                "checklist": [
                    {"id": it.id, "requirement": it.requirement,
                     "evidence_target": it.evidence_target,
                     "addresses_files": list(it.addresses_files),
                     "depends_on": list(it.depends_on), "status": it.status,
                     "milestone": it.milestone, "note": it.note,
                     "scaffold": it.scaffold}
                    for it in checklist.items
                ],
                "open_questions": list(checklist.open_questions),
                "notes": list(checklist.notes),
            },
            indent=2,
        ))
        return 0

    if args.show_prompt:
        print("=== PROMPT ===")
        print(prompt)
        print("=== END PROMPT ===\n")
    print(f"Objective: {args.objective}")
    print(f"done_when: {args.done_when}")
    print(f"Repo: {args.repo or '(none — ungrounded)'}")
    print(f"Checklist: {len(checklist.items)} item(s)   "
          f"{_fmt_usage(latency_ms, usage)}   cognition_calls={ncalls}")
    print()
    for line in _render_checklist(checklist):
        print(line)
    return 0


def _cmd_cognition_breakdown(args) -> int:
    """Dry-run the FULL planning spine end-to-end: scope-grill (the waiter's
    finalize turn) → firming round 1 → decomposer — rendering goal → milestones →
    tasks. Where `plan` and `decompose` each expose ONE link, this walks the whole
    natural chain a durable goal runs over time (create_goal drives the same
    steps), so you can see how a goal composes into milestone-grouped, atomic,
    evidence-targeted tasks. Same no-side-effects contract as `plan`/`decompose`:
    2-3 cognition calls, NO docker / queue / state. Simulates the OpenClaw waiter
    (scope-grill) + the investigating/firming loop, purely for inspection."""
    from dataclasses import replace as _replace

    from .elicitation import build_grill_prompt, validate_step
    from .planner import PlannerError, extract_json
    from .goal.decomposer import GoalDecomposerError, decompose
    from .goal.firmed import derive_done_when
    from .goal.models import Goal
    from .goal.phases.firming import FirmingError, _firm_once, _firmed_extras_block

    workspace_dir = os.path.abspath(os.path.expanduser(args.repo)) if args.repo else os.getcwd()

    async def _run() -> tuple:
        repo_context = await _grounded_context(args.repo)
        stages: list[tuple[str, str, int]] = []
        total_ms = 0
        calls = 0

        # --- 1. spec: the scope-grill finalize turn, or a provided --spec file ---
        if args.spec:
            with open(os.path.expanduser(args.spec), encoding="utf-8") as fh:
                spec = fh.read()
            stages.append(("scope-grill", f"skipped (--spec, {len(spec)} chars)", 0))
        else:
            grill = _default_grill_caller()
            raw, ms, _usage, n = await _traced(
                grill(build_grill_prompt(args.idea, [], finalize=True))
            )
            step = validate_step(json.loads(extract_json(raw)))
            if step["action"] != "done":
                raise PlannerError("scope-grill did not finalize a spec")
            spec = step["spec"]
            total_ms += ms
            calls += n
            stages.append(("scope-grill", f"spec {len(spec)} chars", ms))

        # --- 2. firming round 1 (best-effort — derives done_when + firmed extras) ---
        goal_obj = Goal(
            id="cli-dryrun", objective=args.idea, cadence="1d", engine="devclaw",
            workspace_dir=workspace_dir, done_when=args.done_when or "",
        )
        done_when = args.done_when or ""
        extras = ""
        if not args.no_firm:
            try:
                draft, ms, _usage, n = await _traced(_firm_once(
                    goal_obj, spec=spec, discovery_brief=spec, prior_draft=None,
                    owner_answers=None, round_=1, caller=_default_firming_caller(),
                    repo_context=repo_context or "",
                ))
                total_ms += ms
                calls += n
                done_when = derive_done_when(draft) or done_when
                extras = _firmed_extras_block(draft)
                stages.append(("firming", f"done_when derived ({len(done_when)} chars)", ms))
            except FirmingError as exc:
                stages.append(("firming", f"failed ({exc}); proceeding from spec", 0))

        # --- 3. decompose (spec [+ firmed extras] as the discovery brief) ---
        brief = spec + (("\n" + extras) if extras else "")
        derived = _replace(goal_obj, done_when=done_when or goal_obj.done_when)
        checklist, ms, _usage, n = await _traced(decompose(
            derived, claude_caller=_default_decomposer_caller(),
            discovery_brief=brief, repo_context=repo_context,
        ))
        total_ms += ms
        calls += n
        stages.append(("decompose", f"{len(checklist.items)} items", ms))

        return spec, done_when, checklist, stages, total_ms, calls

    try:
        spec, done_when, checklist, stages, total_ms, calls = asyncio.run(_run())
    except (PlannerError, GoalDecomposerError, OSError) as exc:
        print(f"breakdown failed: {exc}", file=sys.stderr)
        return 1

    milestones: list[str] = []
    for it in checklist.items:
        m = it.milestone or "(none)"
        if m not in milestones:
            milestones.append(m)

    if args.json:
        print(json.dumps(
            {
                "idea": args.idea,
                "spec": spec,
                "done_when": done_when,
                "chain": [{"stage": s, "detail": d, "latency_ms": ms} for s, d, ms in stages],
                "cognition_calls": calls,
                "latency_ms": total_ms,
                "milestones": milestones,
                "checklist": [
                    {"id": it.id, "requirement": it.requirement,
                     "evidence_target": it.evidence_target,
                     "addresses_files": list(it.addresses_files),
                     "depends_on": list(it.depends_on), "status": it.status,
                     "milestone": it.milestone, "note": it.note,
                     "scaffold": it.scaffold}
                    for it in checklist.items
                ],
                "open_questions": list(checklist.open_questions),
                "notes": list(checklist.notes),
            },
            indent=2,
        ))
        return 0

    print(f"Idea: {args.idea}")
    print(f"Repo: {args.repo or '(none — ungrounded)'}")
    if args.show_spec:
        print("\n=== SPEC (from scope-grill) ===")
        print(spec)
        print(f"\n=== derived done_when ===\n{done_when or '(none)'}")
        print("=== END ===\n")
    print("Chain: " + "  →  ".join(f"{s} ({ms} ms)" for s, _d, ms in stages))
    print(f"Result: {len(checklist.items)} task(s) across {len(milestones)} milestone(s)"
          f"   {total_ms} ms total   cognition_calls={calls}   containers=0")
    print()
    for line in _render_checklist(checklist):
        print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devclaw", description="devclaw control-plane CLI")
    sub = parser.add_subparsers(dest="group", required=True)

    p_cog = sub.add_parser(
        "cognition",
        help="dry-run the decomposer/spine — cognition only, no docker/queue/state",
    )
    cog_sub = p_cog.add_subparsers(dest="cmd", required=True)

    c_dec = cog_sub.add_parser(
        "decompose",
        help="decompose a durable objective into a milestone checklist (one decomposer call)",
    )
    c_dec.add_argument("objective", help="the durable objective to decompose")
    c_dec.add_argument("--done-when", required=True,
                       help="the firmed completion criterion the checklist must satisfy")
    c_dec.add_argument("--repo", help="workspace dir to ground the decomposition")
    c_dec.add_argument("-v", "--show-prompt", action="store_true",
                       help="also print the exact prompt sent to the model")
    c_dec.add_argument("--json", action="store_true",
                       help="print the parsed checklist for scripting")
    c_dec.set_defaults(func=lambda reg, get, a: _cmd_cognition_decompose(a))

    c_break = cog_sub.add_parser(
        "breakdown",
        help="walk the FULL spine — scope-grill → firming → decompose — and print "
             "goal → milestones → tasks (2-3 cognition calls, no docker/state)",
    )
    c_break.add_argument("idea", help="the rough project idea / durable goal")
    c_break.add_argument("--done-when", default="",
                         help="seed completion criterion (else firming derives one)")
    c_break.add_argument("--spec",
                         help="skip scope-grill; read a finalized spec.md from this file")
    c_break.add_argument("--no-firm", action="store_true",
                         help="skip firming — go scope-grill → decompose directly")
    c_break.add_argument("--repo", help="workspace dir to ground the chain (REPOSITORY CONTEXT)")
    c_break.add_argument("-v", "--show-spec", action="store_true",
                         help="also print the finalized spec + derived done_when")
    c_break.add_argument("--json", action="store_true",
                         help="print the spec + parsed checklist for scripting")
    c_break.set_defaults(func=lambda reg, get, a: _cmd_cognition_breakdown(a))

    p_score = sub.add_parser(
        "scorecard",
        help="L8 rolling metrics (merge rate, steer rate, verdicts) over a window",
    )
    p_score.add_argument("--window-hours", default=168, type=int,
                         help="lookback window in hours (default 168 = 1 week)")
    p_score.add_argument("--json", action="store_true")
    p_score.set_defaults(func=lambda reg, get, a: _cmd_scorecard(a))

    p_trace = sub.add_parser(
        "trace",
        help="query the traces telemetry table (list events, day-report)",
    )
    tsub = p_trace.add_subparsers(dest="cmd", required=True)

    t_list = tsub.add_parser("list", help="list trace events, newest N, filtered in SQL")
    t_list.add_argument("--goal", help="only this goal's events")
    t_list.add_argument("--kind", help="event kind (cognition, tick, dispatch, subprocess, delivery, notify, trend_check)")
    t_list.add_argument("--role", help="cognition role (planner, evaluator, ...)")
    t_list.add_argument("--since", help="lower bound: 30m/24h/7d or an ISO timestamp (naive=UTC)")
    t_list.add_argument("--errors-only", action="store_true",
                        help="only events whose payload carries a non-empty error")
    t_list.add_argument("--limit", default=100, type=int, help="max events (default 100)")
    t_list.add_argument("--json", action="store_true")
    t_list.set_defaults(func=lambda reg, get, a: _cmd_trace_list(a))

    t_rep = tsub.add_parser(
        "report",
        help="deterministic day-report: tasks, cognition latency by role, retry storms, notifications — no LLM",
    )
    t_rep.add_argument("--since", default="24h",
                       help="window start: 30m/24h/7d or an ISO timestamp (default 24h)")
    t_rep.add_argument("--json", action="store_true")
    t_rep.set_defaults(func=lambda reg, get, a: _cmd_trace_report(a))

    p_sched = sub.add_parser(
        "schedule",
        help="daily run-window (engine-wide or per-goal) that gates NEW dispatch",
    )
    ssub = p_sched.add_subparsers(dest="cmd", required=True)

    s_show = ssub.add_parser("show", help="show the global window + per-goal windows")
    s_show.add_argument("--goal", help="show only this goal's own window")
    s_show.add_argument("--json", action="store_true")
    s_show.set_defaults(func=lambda reg, get, a: _cmd_schedule_show(a))

    s_set = ssub.add_parser("set", help="set the global or (with --goal) a per-goal window")
    s_set.add_argument("--goal", help="target goal id (omit for the engine-wide window)")
    s_set.add_argument("--start", help="window start HH:MM (local to --tz)")
    s_set.add_argument("--end", help="window end HH:MM; may wrap past midnight")
    s_set.add_argument("--tz", help="IANA timezone, e.g. Europe/Kyiv")
    grp = s_set.add_mutually_exclusive_group()
    grp.add_argument("--enable", action="store_true", help="enable the window")
    grp.add_argument("--disable", action="store_true", help="disable (keep times, stop gating)")
    s_set.set_defaults(func=lambda reg, get, a: _cmd_schedule_set(a))

    s_clr = ssub.add_parser("clear", help="remove a window (per-goal falls back to global)")
    s_clr.add_argument("--goal", help="target goal id (omit for the engine-wide window)")
    s_clr.set_defaults(func=lambda reg, get, a: _cmd_schedule_clear(a))

    projects = sub.add_parser("projects", help="manage the project registry")
    psub = projects.add_subparsers(dest="cmd", required=True)

    p_list = psub.add_parser("list", help="list registered projects + live health")
    p_list.add_argument("--status", choices=["active", "paused", "archived"])
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_show = psub.add_parser("show", help="full status of one project")
    p_show.add_argument("id")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_reg = psub.add_parser("register", help="register a new project")
    p_reg.add_argument("id")
    p_reg.add_argument("name")
    p_reg.add_argument("--repo-url")
    p_reg.add_argument("--workspace-dir")
    p_reg.add_argument("--preview-url")
    p_reg.add_argument("--notes")
    p_reg.add_argument("--automerge", choices=["on", "off"],
                        help="pin auto-merge for this project; omit to inherit "
                             "the devclaw-wide default (off)")
    p_reg.add_argument("--merge-strategy", choices=["squash", "merge", "rebase"],
                        help="pin the gh merge strategy; omit to inherit the default")
    p_reg.add_argument("--autodeploy", choices=["on", "off"],
                        help="pin deploy-on-completion; omit to inherit the default")
    p_reg.add_argument("--review-gate", choices=["on", "off"],
                        help="pin the pre-PR review gate; omit to inherit the default")
    p_reg.add_argument("--verify-done", choices=["on", "off"],
                        help="pin the grounded done-gate re-check; omit to inherit the default")
    p_reg.add_argument("--browser-gate-mode", choices=["flexible", "strict"],
                        help="pin the browser-E2E gate stance for a project with no "
                             "Playwright suite (strict forces E2E adoption); omit to "
                             "inherit the default")
    p_reg.set_defaults(func=_cmd_register)

    p_upd = psub.add_parser("update", help="update project fields")
    p_upd.add_argument("id")
    p_upd.add_argument("--name")
    p_upd.add_argument("--repo-url")
    p_upd.add_argument("--workspace-dir")
    p_upd.add_argument("--preview-url")
    p_upd.add_argument("--status", choices=["active", "paused", "archived"])
    p_upd.add_argument("--notes")
    p_upd.add_argument("--automerge", choices=["on", "off", "inherit"],
                        help="'on'/'off' pins auto-merge for this project; "
                             "'inherit' clears a prior override back to the "
                             "devclaw-wide default; omit to leave unchanged")
    p_upd.add_argument("--merge-strategy", choices=["squash", "merge", "rebase", "inherit"],
                        help="pin the gh merge strategy; 'inherit' clears; omit to leave unchanged")
    p_upd.add_argument("--autodeploy", choices=["on", "off", "inherit"],
                        help="pin deploy-on-completion; 'inherit' clears; omit to leave unchanged")
    p_upd.add_argument("--review-gate", choices=["on", "off", "inherit"],
                        help="pin the pre-PR review gate; 'inherit' clears; omit to leave unchanged")
    p_upd.add_argument("--verify-done", choices=["on", "off", "inherit"],
                        help="pin the grounded done-gate re-check; 'inherit' clears; omit to leave unchanged")
    p_upd.add_argument("--browser-gate-mode", choices=["flexible", "strict", "inherit"],
                        help="pin the browser-E2E gate stance (strict forces E2E "
                             "adoption); 'inherit' clears; omit to leave unchanged")
    p_upd.set_defaults(func=_cmd_update)

    p_link = psub.add_parser("link", help="link/unlink a goal to a project")
    p_link.add_argument("id")
    p_link.add_argument("goal_id")
    p_link.add_argument("--unlink", action="store_true")
    p_link.set_defaults(func=_cmd_link)

    p_arch = psub.add_parser("archive", help="archive a project (soft)")
    p_arch.add_argument("id")
    p_arch.set_defaults(func=_cmd_archive)

    p_rm = psub.add_parser("rm", help="delete a project from the registry")
    p_rm.add_argument("id")
    p_rm.set_defaults(func=_cmd_rm)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # The `cognition` dry-run is inspection-only: it constructs NO registry and
    # NO GoalStore (zero state reads/writes, zero queue/engine), so route it
    # before either store is opened. Its func lambdas ignore reg/all_goals.
    if args.group == "cognition":
        return args.func(None, None, args)
    reg = ProjectRegistry(_db_path())
    # All CLI subcommands receive the full goals list for uniformity. Only
    # `list` and `show` actually consume it; the rest ignore it.
    # Share the server's devclaw.db so the CLI reads LIVE goal_status, not a
    # private-DB snapshot. Without state=, GoalStore self-creates its own
    # .goal-state.db, migrates each goal once from the STATUS.md view, then the
    # has_status guard pins that first snapshot — every later `projects list`
    # would show stale status while the server's DB moved on. (T1/PR3.)
    all_goals = _list_goals(GoalStore(_goals_dir(), state=StateStore(_db_path())))
    return args.func(reg, all_goals, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
