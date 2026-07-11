"""devclaw CLI — drive the project registry from a terminal.

The third face of the control plane: chat (MCP tools) and the dashboard (HTTP)
already exist; this is the CLI. It talks to the SAME stores the server uses —
the registry's SQLite table (``DEVCLAW_DB``) and the durable goals
(``DEVCLAW_GOALS_DIR``) — directly and read-mostly, so it works without the
server running and never needs the queue/engine spun up.

Usage:
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
import json
import os
import sys
from typing import Optional

from .goal.store import GoalStore
from .project_registry import ProjectExists, ProjectRegistry, project_rollup
from .state_store import StateStore
from .telemetry import compute_scorecard, format_scorecard


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devclaw", description="devclaw control-plane CLI")
    sub = parser.add_subparsers(dest="group", required=True)

    p_score = sub.add_parser(
        "scorecard",
        help="L8 rolling metrics (merge rate, steer rate, verdicts) over a window",
    )
    p_score.add_argument("--window-hours", default=168, type=int,
                         help="lookback window in hours (default 168 = 1 week)")
    p_score.add_argument("--json", action="store_true")
    p_score.set_defaults(func=lambda reg, get, a: _cmd_scorecard(a))

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
