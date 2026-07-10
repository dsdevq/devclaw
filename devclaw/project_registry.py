"""The project registry — one source of truth for "all the things devclaw owns".

DevClaw already has three lower-level primitives: ephemeral **tasks** and
**programs** (SQLite, in ``state_store``) and durable **goals** (on disk, in
``goal_store``). What it lacked is a single first-class entity that says *"these
are the repos I'm working on, and here's the current status of each"* — the view
a control plane (chat / API / CLI) needs to answer "what are you doing?".

A :class:`Project` is exactly that thin unifying record: a repo + its workspace +
an optional live preview + a status. It does NOT own the goals or duplicate
their state — the rollup (:func:`project_rollup`) joins live goal status on
read via **workspace_dir match**, so the registry never rots.

Association model: a goal belongs to a project iff their ``workspace_dir``
values match (normalized). This deliberately replaces the earlier stored
``goal_ids`` list, which drifted on the cancel-and-refile pattern (v1
missions cancelled but the v2 mission wasn't relinked → Projects Home read
0 active goals for a project that had a live one). Workspace-dir is already
the identity axis for verify / sandbox / PRs, so making it the project↔goal
join key is coherent with the rest of the architecture. ``Project.goal_ids``
is retained as advisory only (CLI ``link_goal`` still works for legacy
compat) but is NOT consulted by the rollup.

Deliberately small and decoupled: its own ``projects`` table on the shared SQLite
file (registry writes are rare + human-driven), no dependency on the goal layer —
the rollup takes a pre-fetched ``all_goals`` list (from
``goal_service.list_goals``) so both the MCP tools and the CLI reuse one shape.
Distinct from ``project_store.Project``, which is the *build-from-scratch
interview* artifact.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .state_store import SQLITE_BUSY_TIMEOUT_MS

ProjectStatus = Literal["active", "paused", "archived"]
#: a read-only getter that returns a goal's live status dict (or raises KeyError);
#: goal_service.get_goal and a GoalStore-backed getter both satisfy it.
GoalGet = Callable[[str], dict]

#: sentinel distinguishing "field not supplied" (leave unchanged) from an
#: explicit ``None`` (clear the override, fall back to the global default).
#: Every per-project OVERRIDE field (``automerge``, ``merge_strategy``,
#: ``autodeploy``, ``review_gate``, ``verify_done``) uses this three-way
#: partial-update semantics.
_UNSET: Any = object()

#: the per-project override fields, in one place so create/read/save/migrate
#: stay in lockstep. Each is nullable = "inherit the devclaw-wide default"; a
#: non-null value pins this project's behaviour regardless of the env default.
#: ``bool`` fields persist as INTEGER (0/1), ``str`` fields as TEXT.
_OVERRIDE_BOOL_FIELDS = ("automerge", "autodeploy", "review_gate", "verify_done")
_OVERRIDE_STR_FIELDS = ("merge_strategy",)
_OVERRIDE_FIELDS = _OVERRIDE_BOOL_FIELDS + _OVERRIDE_STR_FIELDS


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bool_db(value: Optional[bool]) -> Optional[int]:
    """Persist a three-way override bool: None stays NULL (inherit), else 0/1."""
    return None if value is None else int(value)


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
    #: per-project auto-merge override. ``None`` (the default) means "inherit
    #: the devclaw-wide DEVCLAW_GOAL_AUTOMERGE default"; ``True``/``False``
    #: pins this project regardless of the global default. This is the ONLY
    #: place auto-merge is configured — a goal's own goal.yaml has no
    #: automerge field (see devclaw.goal.merge.resolve_automerge).
    automerge: Optional[bool] = None
    #: per-project overrides for delivery/quality knobs that are otherwise
    #: devclaw-wide env defaults. ``None`` = inherit the default; a set value
    #: pins this repo. Same altitude as ``automerge`` — a decision about a
    #: REPO, not a goal's objective. Resolved via :meth:`resolve_override`.
    merge_strategy: Optional[str] = None  # DEVCLAW_GOAL_MERGE_STRATEGY: squash|merge|rebase
    autodeploy: Optional[bool] = None     # DEVCLAW_GOAL_AUTODEPLOY
    review_gate: Optional[bool] = None    # DEVCLAW_REVIEW_GATE
    verify_done: Optional[bool] = None    # DEVCLAW_GOAL_VERIFY_DONE
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
            "automerge": self.automerge,
            "mergeStrategy": self.merge_strategy,
            "autodeploy": self.autodeploy,
            "reviewGate": self.review_gate,
            "verifyDone": self.verify_done,
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
    keys = r.keys()

    def _bool_col(name: str) -> Optional[bool]:
        # Migration-safe: a row read before the column existed has no key.
        raw = r[name] if name in keys else None
        return None if raw is None else bool(raw)

    def _str_col(name: str) -> Optional[str]:
        raw = r[name] if name in keys else None
        return None if raw is None else str(raw)

    return Project(
        id=r["id"],
        name=r["name"],
        repo_url=r["repo_url"],
        workspace_dir=r["workspace_dir"],
        preview_url=r["preview_url"],
        status=r["status"],
        goal_ids=goal_ids,
        notes=r["notes"] or "",
        automerge=_bool_col("automerge"),
        merge_strategy=_str_col("merge_strategy"),
        autodeploy=_bool_col("autodeploy"),
        review_gate=_bool_col("review_gate"),
        verify_done=_bool_col("verify_done"),
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
        # Wait for the lock instead of failing fast — the CLI and the server each
        # open a connection to this shared file, so a CLI write while the server
        # holds the lock must queue, not raise `database is locked`. See
        # state_store.SQLITE_BUSY_TIMEOUT_MS (same env knob, same db file).
        self._db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
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
                  automerge     INTEGER,
                  merge_strategy TEXT,
                  autodeploy    INTEGER,
                  review_gate   INTEGER,
                  verify_done   INTEGER,
                  created_at    INTEGER NOT NULL,
                  updated_at    INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
                """
            )
            # Migration for DBs created before a given override column existed —
            # CREATE TABLE IF NOT EXISTS above is a no-op on an already-existing
            # table, so each per-project override column needs adding explicitly
            # on an older `projects` table. NULL by default = "inherit the global
            # env default", same as a freshly created row. SQLite type is INTEGER
            # for bool fields, TEXT for the string field.
            cols = {row[1] for row in self._db.execute("PRAGMA table_info(projects)")}
            for name in _OVERRIDE_BOOL_FIELDS:
                if name not in cols:
                    self._db.execute(f"ALTER TABLE projects ADD COLUMN {name} INTEGER")
            for name in _OVERRIDE_STR_FIELDS:
                if name not in cols:
                    self._db.execute(f"ALTER TABLE projects ADD COLUMN {name} TEXT")
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
        automerge: Optional[bool] = None,
        merge_strategy: Optional[str] = None,
        autodeploy: Optional[bool] = None,
        review_gate: Optional[bool] = None,
        verify_done: Optional[bool] = None,
    ) -> Project:
        p = Project(
            id=id, name=name, repo_url=repo_url, workspace_dir=workspace_dir,
            preview_url=preview_url, notes=notes, goal_ids=list(goal_ids or []),
            automerge=automerge, merge_strategy=merge_strategy, autodeploy=autodeploy,
            review_gate=review_gate, verify_done=verify_done,
        )
        with self._lock:
            try:
                self._db.execute(
                    """INSERT INTO projects
                         (id, name, repo_url, workspace_dir, preview_url, status,
                          goal_ids, notes, automerge, merge_strategy, autodeploy,
                          review_gate, verify_done, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.id, p.name, p.repo_url, p.workspace_dir, p.preview_url,
                        p.status, json.dumps(p.goal_ids), p.notes,
                        _bool_db(p.automerge), p.merge_strategy, _bool_db(p.autodeploy),
                        _bool_db(p.review_gate), _bool_db(p.verify_done),
                        p.created_at, p.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Roll back the failed INSERT's implicit transaction — otherwise it
                # stays open on this long-lived connection and holds the write lock
                # until the next commit, starving every other connection's writes
                # (the root cause of the 75s `database is locked` stall, found
                # dogfooding 2026-06-21). pysqlite does NOT auto-rollback here.
                self._db.rollback()
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
        automerge: Optional[bool] = _UNSET,
        merge_strategy: Optional[str] = _UNSET,
        autodeploy: Optional[bool] = _UNSET,
        review_gate: Optional[bool] = _UNSET,
        verify_done: Optional[bool] = _UNSET,
    ) -> Project:
        """Partial update — only the supplied fields change. Returns the updated
        project. Raises KeyError if unknown. ``updated_at`` always bumps.

        The per-project override fields (``automerge``, ``merge_strategy``,
        ``autodeploy``, ``review_gate``, ``verify_done``) use three-way
        semantics (unlike the plain fields): omit one entirely to leave the
        current override untouched; pass a concrete value to pin it; pass
        ``None`` explicitly to CLEAR the override back to "inherit the global
        default". The ``_UNSET`` sentinel default is how "don't touch" is
        distinguished from an explicit clear."""
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
        if automerge is not _UNSET:
            p.automerge = automerge
        if merge_strategy is not _UNSET:
            p.merge_strategy = merge_strategy
        if autodeploy is not _UNSET:
            p.autodeploy = autodeploy
        if review_gate is not _UNSET:
            p.review_gate = review_gate
        if verify_done is not _UNSET:
            p.verify_done = verify_done
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
                     goal_ids=?, notes=?, automerge=?, merge_strategy=?, autodeploy=?,
                     review_gate=?, verify_done=?, updated_at=?
                   WHERE id=?""",
                (
                    p.name, p.repo_url, p.workspace_dir, p.preview_url, p.status,
                    json.dumps(p.goal_ids), p.notes,
                    _bool_db(p.automerge), p.merge_strategy, _bool_db(p.autodeploy),
                    _bool_db(p.review_gate), _bool_db(p.verify_done),
                    p.updated_at, p.id,
                ),
            )
            self._db.commit()

    def find_by_workspace_dir(self, workspace_dir: Optional[str]) -> Optional[Project]:
        """The reverse of the rollup join: given a goal's ``workspace_dir``,
        find the project that owns it (normalized match, same rule as
        :func:`project_rollup`). Returns ``None`` if the workspace is empty or
        no project claims it. Used by the auto-merge resolver — a goal has no
        automerge setting of its own, only its owning project does."""
        target = _normalize_workspace(workspace_dir)
        if target is None:
            return None
        with self._lock:
            rows = self._db.execute("SELECT * FROM projects").fetchall()
        for r in rows:
            if _normalize_workspace(r["workspace_dir"]) == target:
                return _row_to_project(r)
        return None

    def resolve_override(self, workspace_dir: Optional[str], field: str, default: Any) -> Any:
        """Resolve one per-project override for a goal/task working in
        ``workspace_dir``: the owning project's value for ``field`` if it pins
        one (non-null), else ``default`` (the devclaw-wide env default). This is
        the single generic seam every override consumer routes through — a goal
        has no such setting of its own, only its owning project does, and when
        the fleet-wide settings store lands (PR B) only ``default``'s source
        changes here, not the call sites.

        ``field`` must be one of :data:`_OVERRIDE_FIELDS`; anything else is a
        programming error and raises, rather than silently returning the
        default and masking a typo."""
        if field not in _OVERRIDE_FIELDS:
            raise ValueError(f"not a per-project override field: {field!r}")
        project = self.find_by_workspace_dir(workspace_dir)
        if project is not None:
            value = getattr(project, field)
            if value is not None:
                return value
        return default


def _normalize_workspace(path: Optional[str]) -> Optional[str]:
    """Canonicalize workspace paths for join purposes: strip trailing slash,
    collapse duplicate slashes, expand user. Stays purely string-shaped — we
    do NOT hit the filesystem here (projects may point at paths that don't
    exist on this host, e.g. the CLI reading a VPS registry snapshot)."""
    if not path:
        return None
    p = str(path).strip()
    if not p:
        return None
    # Expand a leading ~ without resolving symlinks/existence.
    if p.startswith("~"):
        p = str(Path(p).expanduser())
    # Collapse `//` runs and drop any trailing slash (except root).
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


def project_rollup(project: Project, all_goals: list[dict]) -> dict:
    """Join a project with live goal state via workspace_dir match.

    ``all_goals`` is the pre-fetched output of ``goal_service.list_goals()``
    (or the CLI's GoalStore-backed equivalent). Every goal whose normalized
    ``workspace_dir`` equals the project's normalized ``workspace_dir`` is
    associated. Passing the full list in from the caller lets us render every
    project in a single ``list_goals`` scan instead of an N-times per-project
    fetch.

    ``health`` is a cheap derived signal for the control plane: ``blocked`` if
    any goal is blocked or flagged stalled by the watchdog, ``done`` if all
    goals are done, ``working`` if any is active, else ``idle``."""
    proj_ws = _normalize_workspace(project.workspace_dir)
    goals: list[dict] = []
    if proj_ws is not None:
        for g in all_goals:
            if _normalize_workspace(g.get("workspace_dir")) != proj_ws:
                continue
            goals.append(
                {
                    "id": g.get("id"),
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
