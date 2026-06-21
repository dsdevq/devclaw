"""The project registry — one source of truth for "all the things devclaw owns".

DevClaw already has three lower-level primitives: ephemeral **tasks** and
**programs** (SQLite, in ``state_store``) and durable **goals** (on disk, in
``goal_store``). What it lacked is a single first-class entity that says *"these
are the repos I'm working on, and here's the current status of each"* — the view
a control plane (chat / API / CLI) needs to answer "what are you doing?".

A :class:`Project` is exactly that thin unifying record: a repo + its workspace +
an optional live preview + a status + the goal(s) driving it. It does NOT own the
goals or duplicate their state — it *links* to them by id and the rollup
(:func:`project_rollup`) joins live goal status on read, so the registry never
rots (it stores facts: name, repo, preview url; not "phase", which it reads live).

Deliberately small and decoupled: its own ``projects`` table on the shared SQLite
file (registry writes are rare + human-driven), no dependency on the goal layer —
the rollup takes a ``goal_get`` callable so both the MCP tools (goal_service) and
the CLI (a GoalStore-backed getter) reuse one shape. Distinct from
``project_store.Project``, which is the *build-from-scratch interview* artifact.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

ProjectStatus = Literal["active", "paused", "archived"]
#: a read-only getter that returns a goal's live status dict (or raises KeyError);
#: goal_service.get_goal and a GoalStore-backed getter both satisfy it.
GoalGet = Callable[[str], dict]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Project:
    id: str  # stable slug, e.g. "todo-fullstack-demo"
    name: str
    repo_url: Optional[str] = None
    workspace_dir: Optional[str] = None
    preview_url: Optional[str] = None
    status: ProjectStatus = "active"
    #: durable goals driving this project — linked by id, never copied
    goal_ids: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "repoUrl": self.repo_url,
            "workspaceDir": self.workspace_dir,
            "previewUrl": self.preview_url,
            "status": self.status,
            "goalIds": list(self.goal_ids),
            "notes": self.notes,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def _row_to_project(r: sqlite3.Row) -> Project:
    goal_ids: list[str] = []
    if r["goal_ids"]:
        try:
            parsed = json.loads(r["goal_ids"])
            if isinstance(parsed, list):
                goal_ids = [x for x in parsed if isinstance(x, str)]
        except json.JSONDecodeError:
            pass  # tolerate a corrupt cell — treat as no links
    return Project(
        id=r["id"],
        name=r["name"],
        repo_url=r["repo_url"],
        workspace_dir=r["workspace_dir"],
        preview_url=r["preview_url"],
        status=r["status"],
        goal_ids=goal_ids,
        notes=r["notes"] or "",
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


class ProjectExists(Exception):
    """Raised on create() when the id is already taken."""


class ProjectRegistry:
    """SQLite-backed CRUD for the project registry. Owns its own ``projects``
    table on the given db file (shared with the state store; registry writes are
    infrequent so a second WAL connection is fine). A re-entrant lock serializes
    access since FastMCP may touch it from the loop and background tasks."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._bootstrap()

    def _bootstrap(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                  id            TEXT PRIMARY KEY,
                  name          TEXT NOT NULL,
                  repo_url      TEXT,
                  workspace_dir TEXT,
                  preview_url   TEXT,
                  status        TEXT NOT NULL DEFAULT 'active',
                  goal_ids      TEXT,
                  notes         TEXT,
                  created_at    INTEGER NOT NULL,
                  updated_at    INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
                """
            )
            self._db.commit()

    # ---- CRUD --------------------------------------------------------------

    def create(
        self,
        *,
        id: str,
        name: str,
        repo_url: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        preview_url: Optional[str] = None,
        notes: str = "",
        goal_ids: Optional[list[str]] = None,
    ) -> Project:
        p = Project(
            id=id, name=name, repo_url=repo_url, workspace_dir=workspace_dir,
            preview_url=preview_url, notes=notes, goal_ids=list(goal_ids or []),
        )
        with self._lock:
            try:
                self._db.execute(
                    """INSERT INTO projects
                         (id, name, repo_url, workspace_dir, preview_url, status,
                          goal_ids, notes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.id, p.name, p.repo_url, p.workspace_dir, p.preview_url,
                        p.status, json.dumps(p.goal_ids), p.notes,
                        p.created_at, p.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectExists(id) from exc
            self._db.commit()
        return p

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return _row_to_project(r) if r else None

    def list(self, *, status: Optional[ProjectStatus] = None) -> list[Project]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM projects ORDER BY updated_at DESC"
                ).fetchall()
        return [_row_to_project(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        repo_url: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        preview_url: Optional[str] = None,
        status: Optional[ProjectStatus] = None,
        notes: Optional[str] = None,
    ) -> Project:
        """Partial update — only the supplied fields change. Returns the updated
        project. Raises KeyError if unknown. ``updated_at`` always bumps."""
        p = self.get(project_id)
        if p is None:
            raise KeyError(project_id)
        if name is not None:
            p.name = name
        if repo_url is not None:
            p.repo_url = repo_url
        if workspace_dir is not None:
            p.workspace_dir = workspace_dir
        if preview_url is not None:
            p.preview_url = preview_url
        if status is not None:
            p.status = status
        if notes is not None:
            p.notes = notes
        p.updated_at = _now_ms()
        self._save(p)
        return p

    def link_goal(self, project_id: str, goal_id: str) -> Project:
        """Attach a goal to the project (idempotent). Raises KeyError if unknown."""
        p = self.get(project_id)
        if p is None:
            raise KeyError(project_id)
        if goal_id not in p.goal_ids:
            p.goal_ids.append(goal_id)
            p.updated_at = _now_ms()
            self._save(p)
        return p

    def unlink_goal(self, project_id: str, goal_id: str) -> Project:
        p = self.get(project_id)
        if p is None:
            raise KeyError(project_id)
        if goal_id in p.goal_ids:
            p.goal_ids.remove(goal_id)
            p.updated_at = _now_ms()
            self._save(p)
        return p

    def delete(self, project_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._db.commit()
            return cur.rowcount == 1

    def _save(self, p: Project) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE projects SET
                     name=?, repo_url=?, workspace_dir=?, preview_url=?, status=?,
                     goal_ids=?, notes=?, updated_at=?
                   WHERE id=?""",
                (
                    p.name, p.repo_url, p.workspace_dir, p.preview_url, p.status,
                    json.dumps(p.goal_ids), p.notes, p.updated_at, p.id,
                ),
            )
            self._db.commit()


def project_rollup(project: Project, goal_get: GoalGet) -> dict:
    """Join the project's stored facts with the LIVE status of each linked goal.

    The registry never caches goal phase (that rots); the rollup reads it on
    demand via ``goal_get`` (goal_service.get_goal or a GoalStore-backed getter).
    A linked goal that no longer exists is surfaced as ``{"missing": true}`` rather
    than dropped, so a dangling link is visible instead of silently hidden.

    ``health`` is a cheap derived signal for the control plane: ``blocked`` if any
    goal is blocked or flagged stalled by the watchdog, ``done`` if all goals are
    done, ``working`` if any is active, else ``idle``."""
    goals: list[dict] = []
    for gid in project.goal_ids:
        try:
            g = goal_get(gid)
        except KeyError:
            goals.append({"id": gid, "missing": True})
            continue
        goals.append(
            {
                "id": gid,
                "phase": g.get("phase"),
                "lifecycle": g.get("lifecycle"),
                "blocked_on": g.get("blocked_on"),
                "progress": g.get("progress"),
                "direction": g.get("direction"),
            }
        )
    out = project.to_dict()
    out["goals"] = goals
    out["health"] = _health(project.status, goals)
    return out


def _health(status: ProjectStatus, goals: list[dict]) -> str:
    if status == "archived":
        return "archived"
    live = [g for g in goals if not g.get("missing")]
    if not live:
        return "idle"
    phases = [g.get("phase") for g in live]
    stalled = any((g.get("progress") or {}).get("stalled") for g in live)
    if "blocked" in phases or stalled:
        return "blocked"
    if all(p == "done" for p in phases):
        return "done"
    if any(p in ("in_flight", "verifying", "idle") for p in phases):
        return "working"
    return "idle"
