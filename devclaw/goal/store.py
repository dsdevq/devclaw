"""The durable mind on disk — reusing the vault ``projects/`` convention.

Folded in from goalclaw. Layout per goal, under ``<goals_dir>/<goal_id>/``:
  goal.yaml      FACTS    — objective, cadence, engine, workspace_dir, done_when, backlog
  STATUS.md      STATE    — machine state in YAML frontmatter, overwritten each tick
  log.md         EVENTS   — append-only, newest at bottom
  inbox.md       STEERING — append-only direction (from Denys OR the evaluator); cursor-consumed
  deliveries.md  EVIDENCE — append-only, grounded record of what each action actually
                            shipped (agent summary + gate verdict + PR), read by the evaluator

No database: the filesystem IS the store, git-synced like the rest of the vault.
A clock is injected (``now``) so ticks are deterministic under test.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .models import Goal, GoalStatus, InFlight

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    """'6h' / '1d' / '30m' / '90s' → seconds. Raises ValueError on garbage."""
    m = _DURATION.match(s or "")
    if not m:
        raise ValueError(f"bad cadence {s!r}; want <int><s|m|h|d>")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalStore:
    def __init__(self, goals_dir: Path, *, now: Callable[[], datetime] = _default_now) -> None:
        self._root = Path(goals_dir)
        self._now = now

    # ---- discovery ---------------------------------------------------------

    def list_goal_ids(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if (p / "goal.yaml").is_file())

    def _dir(self, goal_id: str) -> Path:
        return self._root / goal_id

    def exists(self, goal_id: str) -> bool:
        return (self._dir(goal_id) / "goal.yaml").is_file()

    # ---- goal (facts) ------------------------------------------------------

    def create_goal(
        self,
        goal_id: str,
        *,
        objective: str,
        workspace_dir: str,
        cadence: str = "1d",
        repo_url: str | None = None,
        verify_cmd: str | None = None,
        open_pr: bool = True,
        done_when: str = "",
        backlog: list[str] | None = None,
    ) -> Goal:
        """Write a new goal.yaml. Raises FileExistsError if the id is taken."""
        if self.exists(goal_id):
            raise FileExistsError(f"goal {goal_id!r} already exists")
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "goal.yaml").write_text(
            yaml.safe_dump(
                {
                    "objective": objective.strip(),
                    "cadence": cadence,
                    "engine": "devclaw",
                    "workspace_dir": workspace_dir,
                    "repo_url": repo_url,
                    "verify_cmd": verify_cmd,
                    "open_pr": open_pr,
                    "done_when": done_when.strip(),
                    "backlog": list(backlog or []),
                },
                sort_keys=False,
            )
        )
        return self.load_goal(goal_id)

    def load_goal(self, goal_id: str) -> Goal:
        raw = yaml.safe_load((self._dir(goal_id) / "goal.yaml").read_text()) or {}
        return Goal(
            id=goal_id,
            objective=str(raw["objective"]).strip(),
            cadence=str(raw.get("cadence", "1d")),
            engine=raw.get("engine", "devclaw"),
            workspace_dir=str(raw["workspace_dir"]),
            repo_url=(str(raw["repo_url"]) if raw.get("repo_url") else None),
            verify_cmd=raw.get("verify_cmd") or None,
            open_pr=bool(raw.get("open_pr", True)),
            done_when=str(raw.get("done_when", "")).strip(),
            backlog=[str(x).strip() for x in (raw.get("backlog") or [])],
        )

    # ---- status (state) ----------------------------------------------------

    def load_status(self, goal_id: str) -> GoalStatus:
        path = self._dir(goal_id) / "STATUS.md"
        if not path.exists():
            return GoalStatus()
        fm = self._read_frontmatter(path.read_text())
        inflight = None
        if fm.get("in_flight"):
            f = fm["in_flight"]
            raw_addr = f.get("addresses") or []
            addresses = (
                [str(a) for a in raw_addr if str(a).strip()]
                if isinstance(raw_addr, list) else []
            )
            inflight = InFlight(
                engine=f["engine"], tool=f["tool"], id=f["id"],
                ref_kind=f["ref_kind"], goal=f.get("goal", ""),
                is_done_check=bool(f.get("is_done_check", False)),
                is_discovery=bool(f.get("is_discovery", False)),
                addresses=addresses,
            )
        return GoalStatus(
            phase=fm.get("phase", "idle"),
            lifecycle=fm.get("lifecycle") or None,
            in_flight=inflight,
            blocked_on=fm.get("blocked_on") or None,
            next=fm.get("next", "") or "",
            last_plan_at=fm.get("last_plan_at") or None,
            last_tick_at=fm.get("last_tick_at") or None,
            inbox_cursor=int(fm.get("inbox_cursor", 0)),
            actions_dispatched=int(fm.get("actions_dispatched", 0)),
            deliveries_since_eval=int(fm.get("deliveries_since_eval", 0)),
            last_eval_verdict=fm.get("last_eval_verdict") or None,
            last_eval_at=fm.get("last_eval_at") or None,
            last_eval_note=fm.get("last_eval_note", "") or "",
            last_progress_at=fm.get("last_progress_at") or None,
            no_progress_notified=bool(fm.get("no_progress_notified", False)),
        )

    def save_status(self, goal_id: str, status: GoalStatus) -> None:
        fm: dict = {
            "phase": status.phase,
            "lifecycle": status.lifecycle,
            "in_flight": (
                {
                    "engine": status.in_flight.engine,
                    "tool": status.in_flight.tool,
                    "id": status.in_flight.id,
                    "ref_kind": status.in_flight.ref_kind,
                    "goal": status.in_flight.goal,
                    "is_done_check": status.in_flight.is_done_check,
                    "is_discovery": status.in_flight.is_discovery,
                    "addresses": list(status.in_flight.addresses),
                }
                if status.in_flight
                else None
            ),
            "blocked_on": status.blocked_on,
            "next": status.next,
            "last_plan_at": status.last_plan_at,
            "last_tick_at": status.last_tick_at,
            "inbox_cursor": status.inbox_cursor,
            "actions_dispatched": status.actions_dispatched,
            "deliveries_since_eval": status.deliveries_since_eval,
            "last_eval_verdict": status.last_eval_verdict,
            "last_eval_at": status.last_eval_at,
            "last_eval_note": status.last_eval_note,
            "last_progress_at": status.last_progress_at,
            "no_progress_notified": status.no_progress_notified,
        }
        body = self._render_status_body(goal_id, status)
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n---\n\n" + body
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "STATUS.md").write_text(text)

    # ---- log (events) ------------------------------------------------------

    def append_log(self, goal_id: str, message: str) -> None:
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "log.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — log\n\n")
        with path.open("a") as fh:
            fh.write(f"- [{self._now().isoformat(timespec='seconds')}] {message}\n")

    def recent_log(self, goal_id: str, n: int = 20) -> str:
        path = self._dir(goal_id) / "log.md"
        if not path.exists():
            return ""
        lines = [ln for ln in path.read_text().splitlines() if ln.startswith("- [")]
        return "\n".join(lines[-n:])

    # ---- deliveries (grounded evidence for the evaluator) ------------------

    def append_delivery(self, goal_id: str, instruction: str, body: str) -> None:
        """Append a grounded record of what one action actually shipped — the
        agent's own summary + the gate verdict + the PR url, captured in-process
        from the full task row (not the old over-the-wire blob). This is the
        substrate the direction evaluator reads to judge shipped-vs-correct."""
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "deliveries.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — deliveries (what each action shipped)\n\n")
        ts = self._now().isoformat(timespec="seconds")
        with path.open("a") as fh:
            fh.write(f"## [{ts}] {instruction}\n\n{body.strip()}\n\n")

    def write_discovery(self, goal_id: str, brief: str) -> None:
        """Persist the ``investigating`` phase's discovery brief (current state ·
        gap-to-good · best-practice checklist) as a durable artifact the planner
        and evaluator draw on. Overwritten if investigation re-runs."""
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        ts = self._now().isoformat(timespec="seconds")
        (d / "discovery.md").write_text(
            f"# {goal_id} — discovery brief\n\n_generated {ts}_\n\n{brief.strip()}\n"
        )

    def read_discovery(self, goal_id: str) -> str:
        """The discovery brief, or '' if the investigating phase hasn't run."""
        path = self._dir(goal_id) / "discovery.md"
        return path.read_text() if path.exists() else ""

    # ---- checklist (decomposer output — the durable structured plan) ------

    def write_checklist(self, goal_id: str, checklist: "Checklist") -> None:  # type: ignore[name-defined]
        """Persist the decomposer's full output as ``checklist.yaml``. Lives
        next to ``STATUS.md`` and is the source of truth the per-tick planner
        picks actions from; mutable across ticks (settle hook + steer can
        rewrite items)."""
        from .checklist import dump_checklist

        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "checklist.yaml").write_text(dump_checklist(checklist))

    def read_checklist(self, goal_id: str) -> "Checklist | None":  # type: ignore[name-defined]
        """The current checklist, or ``None`` if the decomposer hasn't run
        yet (legacy goals + brand-new goals before the decomposing phase
        completes). The per-tick planner falls back to backlog-driven mode
        when this is ``None``."""
        from .checklist import ChecklistParseError, parse_checklist

        path = self._dir(goal_id) / "checklist.yaml"
        if not path.exists():
            return None
        try:
            return parse_checklist(path.read_text())
        except ChecklistParseError:
            return None  # corrupted on disk — caller treats as absent

    # ---- scope spec (handed in by the waiter via create_goal) ---------------

    def write_spec(self, goal_id: str, spec: str) -> None:
        """Persist the agreed scope spec — what to build, what's out, constraints.
        Produced by the OpenClaw waiter's scope_grill conversation BEFORE the goal
        is created, passed in through create_goal, and read by the evaluator so
        done is judged against the shared contract."""
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        ts = self._now().isoformat(timespec="seconds")
        (d / "spec.md").write_text(f"# {goal_id} — spec\n\n_agreed {ts}_\n\n{spec.strip()}\n")

    def read_spec(self, goal_id: str) -> str:
        path = self._dir(goal_id) / "spec.md"
        return path.read_text() if path.exists() else ""

    def recent_deliveries(self, goal_id: str, chars: int = 8000) -> str:
        """The tail of deliveries.md (bounded — the evaluator's grounding context)."""
        path = self._dir(goal_id) / "deliveries.md"
        if not path.exists():
            return ""
        text = path.read_text()
        return text[-chars:] if len(text) > chars else text

    # ---- inbox (steering) --------------------------------------------------

    def _inbox_lines(self, goal_id: str) -> list[str]:
        path = self._dir(goal_id) / "inbox.md"
        if not path.exists():
            return []
        out = []
        for ln in path.read_text().splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                out.append(s)
        return out

    def unread_steering(self, goal_id: str, status: GoalStatus) -> str:
        lines = self._inbox_lines(goal_id)
        fresh = lines[status.inbox_cursor :]
        return "\n".join(fresh).strip()

    def steering_cursor(self, goal_id: str) -> int:
        return len(self._inbox_lines(goal_id))

    def append_steering(self, goal_id: str, lines: list[str], *, source: str = "denys") -> None:
        """Append steering lines to inbox.md. Used by the steer_goal tool (source
        'denys') AND by the direction evaluator writing corrections (source
        'auto-eval') — the evaluator steers the goal the same way Denys would, so
        the next-action planner picks it up through the one steering path."""
        clean = [ln.strip() for ln in lines if ln.strip()]
        if not clean:
            return
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "inbox.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — inbox (steering)\n\n")
        ts = self._now().isoformat(timespec="seconds")
        with path.open("a") as fh:
            for ln in clean:
                fh.write(f"- [{source} {ts}] {ln}\n")

    # ---- helpers -----------------------------------------------------------

    def cadence_due(self, goal: Goal, status: GoalStatus) -> bool:
        if status.last_plan_at is None:
            return True
        try:
            last = datetime.fromisoformat(status.last_plan_at)
        except ValueError:
            return True
        return (self._now() - last).total_seconds() >= parse_duration(goal.cadence)

    def now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def seconds_since(self, iso_ts: str | None) -> float | None:
        """Wall-clock seconds between ``iso_ts`` and now (injected clock). None if
        the timestamp is missing or unparseable — the caller treats that as 'no
        baseline yet', never as 'zero elapsed'. Used by the no-progress watchdog."""
        if not iso_ts:
            return None
        try:
            then = datetime.fromisoformat(iso_ts)
        except ValueError:
            return None
        return (self._now() - then).total_seconds()

    @staticmethod
    def _read_frontmatter(text: str) -> dict:
        m = _FRONTMATTER.match(text)
        if not m:
            return {}
        return yaml.safe_load(m.group(1)) or {}

    @staticmethod
    def _render_status_body(goal_id: str, s: GoalStatus) -> str:
        if s.phase in ("in_flight", "verifying") and s.in_flight:
            verb = "verifying done via" if s.phase == "verifying" else "running"
            head = f"{verb} `{s.in_flight.tool}` ({s.in_flight.id})"
        elif s.phase == "blocked":
            head = f"blocked — {s.blocked_on}"
        else:
            head = s.phase
        lines = [f"# {goal_id} — status", "", f"**phase:** {head}"]
        if s.next:
            lines.append(f"**next:** {s.next}")
        if s.last_eval_verdict:
            lines.append(f"**direction:** {s.last_eval_verdict} — {s.last_eval_note}")
        if s.last_tick_at:
            lines.append(f"\n_updated {s.last_tick_at}_")
        return "\n".join(lines) + "\n"
