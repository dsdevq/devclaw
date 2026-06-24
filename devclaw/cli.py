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


def _db_path() -> str:
    return os.path.abspath(os.environ.get("DEVCLAW_DB", "devclaw.db"))


def _goals_dir() -> str:
    return os.path.expanduser(os.environ.get("DEVCLAW_GOALS_DIR", "~/memory/goals"))


def _goal_getter(goal_store: GoalStore):
    """A read-only goal-status getter (shape matches goal_service.get_goal's
    relevant fields) built straight from the GoalStore — no queue/engine needed."""

    def get(goal_id: str) -> dict:
        if not goal_store.exists(goal_id):
            raise KeyError(goal_id)
        s = goal_store.load_status(goal_id)
        return {
            "phase": s.phase,
            "lifecycle": s.lifecycle or "executing",
            "blocked_on": s.blocked_on,
            "progress": {"last_at": s.last_progress_at, "stalled": s.no_progress_notified},
            "direction": (
                {"verdict": s.last_eval_verdict, "at": s.last_eval_at, "note": s.last_eval_note}
                if s.last_eval_verdict else None
            ),
        }

    return get


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


def _cmd_list(reg: ProjectRegistry, goal_get, args) -> int:
    items = [project_rollup(p, goal_get) for p in reg.list(status=args.status)]
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("no projects registered")
        return 0
    for p in items:
        print(_fmt_project_line(p))
    return 0


def _cmd_show(reg: ProjectRegistry, goal_get, args) -> int:
    p = reg.get(args.id)
    if p is None:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    rolled = project_rollup(p, goal_get)
    if args.json:
        print(json.dumps(rolled, indent=2))
    else:
        _print_show(rolled)
    return 0


def _cmd_register(reg: ProjectRegistry, goal_get, args) -> int:
    try:
        p = reg.create(
            id=args.id, name=args.name, repo_url=args.repo_url,
            workspace_dir=args.workspace_dir, preview_url=args.preview_url,
            notes=args.notes or "",
        )
    except ProjectExists:
        print(f"project already exists: {args.id}", file=sys.stderr)
        return 1
    print(f"registered {p.id}")
    return 0


def _cmd_update(reg: ProjectRegistry, goal_get, args) -> int:
    try:
        reg.update(
            args.id, name=args.name, repo_url=args.repo_url,
            workspace_dir=args.workspace_dir, preview_url=args.preview_url,
            status=args.status, notes=args.notes,
        )
    except KeyError:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    print(f"updated {args.id}")
    return 0


def _cmd_link(reg: ProjectRegistry, goal_get, args) -> int:
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


def _cmd_archive(reg: ProjectRegistry, goal_get, args) -> int:
    try:
        reg.update(args.id, status="archived")
    except KeyError:
        print(f"unknown project: {args.id}", file=sys.stderr)
        return 1
    print(f"archived {args.id}")
    return 0


def _cmd_rm(reg: ProjectRegistry, goal_get, args) -> int:
    if reg.delete(args.id):
        print(f"removed {args.id}")
        return 0
    print(f"unknown project: {args.id}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devclaw", description="devclaw control-plane CLI")
    sub = parser.add_subparsers(dest="group", required=True)

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
    p_reg.set_defaults(func=_cmd_register)

    p_upd = psub.add_parser("update", help="update project fields")
    p_upd.add_argument("id")
    p_upd.add_argument("--name")
    p_upd.add_argument("--repo-url")
    p_upd.add_argument("--workspace-dir")
    p_upd.add_argument("--preview-url")
    p_upd.add_argument("--status", choices=["active", "paused", "archived"])
    p_upd.add_argument("--notes")
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
    goal_get = _goal_getter(GoalStore(_goals_dir()))
    return args.func(reg, goal_get, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
