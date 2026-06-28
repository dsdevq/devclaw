"""devclaw CLI — drive the project registry + visual-judge from a terminal.

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

  python -m devclaw.cli visual-judge <workspace> [--rubric path] [--against-head] [--json]

Output is human-readable by default; pass ``--json`` to list/show/visual-judge
for the raw rollup (the same shape the MCP tools return), so the CLI is
scriptable too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
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


# ---- visual-judge: smoke the gate against an arbitrary workspace ----------

#: the well-known evidence subdir the runner-side gate writes to (same path).
_VISUAL_EVIDENCE_SUBDIR = ".devclaw-evidence"


def _run_local_visual_verify(workspace_dir: str, timeout: int) -> dict:
    """Host-side mirror of the runner's `_run_visual_verify`. Inlined rather
    than imported so devclaw stays decoupled from openhands-runner (which is
    not a Python package — model-agnostic invariant). Same JSON contract on
    stdout: ``{routes: [...], notes}``."""
    script = os.path.join(workspace_dir, ".agent", "visual-verify.sh")
    if not os.path.exists(script):
        return {"ran": False, "manifest": [], "errors": [], "reason": "no script"}
    if not os.access(script, os.X_OK):
        return {
            "ran": False, "manifest": [], "errors": [],
            "reason": ".agent/visual-verify.sh is not executable",
        }
    evidence_dir = os.path.join(workspace_dir, _VISUAL_EVIDENCE_SUBDIR)
    try:
        os.makedirs(evidence_dir, exist_ok=True)
    except OSError as exc:
        return {
            "ran": False, "manifest": [], "errors": [f"evidence dir: {exc}"],
            "reason": "evidence dir create failed",
        }
    env = dict(os.environ)
    env["DEVCLAW_VISUAL_EVIDENCE_DIR"] = evidence_dir
    env["DEVCLAW_TASK_KIND"] = "cli"
    env["DEVCLAW_TASK_ID"] = "cli"
    try:
        proc = subprocess.run(
            ["bash", script],
            cwd=workspace_dir, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ran": True, "manifest": [], "reason": "timeout",
                "errors": [f"visual-verify timed out after {timeout}s"]}
    except OSError as exc:
        return {"ran": True, "manifest": [], "reason": "spawn failed",
                "errors": [f"failed to run visual-verify: {exc}"]}
    if proc.returncode != 0:
        return {"ran": True, "manifest": [], "reason": f"exit {proc.returncode}",
                "errors": [f"visual-verify exited {proc.returncode}",
                           (proc.stderr or "")[-2000:]]}
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {"ran": True, "manifest": [], "reason": "empty manifest",
                "errors": ["visual-verify printed nothing to stdout"]}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"ran": True, "manifest": [], "reason": "bad json",
                "errors": [f"visual-verify stdout was not JSON: {exc}", stdout[-2000:]]}
    if not isinstance(parsed, dict):
        return {"ran": True, "manifest": [], "reason": "bad shape",
                "errors": ["visual-verify JSON root must be an object"]}
    routes = parsed.get("routes")
    manifest: list[dict] = []
    if isinstance(routes, list):
        for entry in routes:
            if isinstance(entry, dict):
                manifest.append(entry)
    return {"ran": True, "manifest": manifest, "errors": [],
            "reason": "ok" if manifest else "empty routes",
            "notes": str(parsed.get("notes") or "").strip()}


def _read_rubric(workspace_dir: str, override: Optional[str]) -> str:
    path = override or os.path.join(workspace_dir, ".agent", "visual-rubric.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _diff_against_head(workspace_dir: str) -> str:
    """`git diff HEAD~1` for the workspace; empty string on any failure (not a
    repo, no parent, git missing)."""
    try:
        p = subprocess.run(
            ["git", "-C", workspace_dir, "diff", "HEAD~1"],
            capture_output=True, text=True, timeout=15,
        )
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _cmd_visual_judge(reg, goal_get, args) -> int:
    """Run the visual gate against a workspace checkpoint. Exit codes:
    0 approve, 1 request_changes, 2 infrastructure error (script missing,
    judge unreachable, parse error)."""
    workspace = os.path.abspath(args.workspace)
    if not os.path.isdir(workspace):
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2
    timeout = int(os.environ.get("DEVCLAW_VISUAL_TIMEOUT_S", "180"))
    evidence = _run_local_visual_verify(workspace, timeout=timeout)
    if not evidence.get("ran"):
        print(f"visual-verify did not run: {evidence.get('reason')}", file=sys.stderr)
        return 2
    if not evidence.get("manifest"):
        msg = f"visual-verify produced no manifest: {evidence.get('reason')}"
        if evidence.get("errors"):
            msg += "\n  errors: " + "\n  ".join(evidence["errors"])
        print(msg, file=sys.stderr)
        return 2
    rubric = _read_rubric(workspace, args.rubric)
    diff = _diff_against_head(workspace) if args.against_head else ""

    from .quality.visual_judge import judge_screenshots

    evidence_dir = os.path.join(workspace, _VISUAL_EVIDENCE_SUBDIR)
    try:
        verdict = asyncio.run(judge_screenshots(
            goal=args.goal or "(no ticket — CLI smoke)",
            kind="implement_feature",
            diff=diff,
            manifest=evidence["manifest"],
            evidence_dir=evidence_dir,
            rubric_per_repo=rubric,
        ))
    except Exception as err:  # noqa: BLE001 — surface judge errors as infra failures
        print(f"visual judge failed: {err}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        print(f"verdict: {verdict['verdict']}")
        if verdict.get("summary"):
            print(f"summary: {verdict['summary']}")
        for i in verdict.get("issues", []):
            loc = f" [{i['location']}]" if i.get("location") else ""
            print(f"  ({i['severity']}){loc} {i['problem']}")
            if i.get("fix"):
                print(f"    fix: {i['fix']}")
    return 0 if verdict["verdict"] == "approve" else 1


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

    vj = sub.add_parser(
        "visual-judge",
        help="run the visual-evidence gate against a workspace checkpoint",
    )
    vj.add_argument("workspace", help="path to the workspace whose .agent/visual-verify.sh runs")
    vj.add_argument("--rubric", help="override per-repo rubric path (default: <workspace>/.agent/visual-rubric.md)")
    vj.add_argument("--against-head", action="store_true",
                    help="include `git diff HEAD~1` as the diff context for the judge")
    vj.add_argument("--goal", default="", help="ticket text describing the change (defaults to a CLI-smoke note)")
    vj.add_argument("--json", action="store_true", help="emit the full verdict dict")
    vj.set_defaults(func=_cmd_visual_judge, _needs_registry=False)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Lazy: only spin up the project registry + goal store for commands that
    # actually need them. visual-judge runs against an arbitrary workspace and
    # must not create a stray devclaw.db in its CWD.
    needs_registry = getattr(args, "_needs_registry", True)
    if needs_registry:
        reg = ProjectRegistry(_db_path())
        goal_get = _goal_getter(GoalStore(_goals_dir()))
    else:
        reg, goal_get = None, None
    return args.func(reg, goal_get, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
