"""The durable mind on disk — reusing the vault ``projects/`` convention.

Folded in from goalclaw. Layout per goal, under ``<goals_dir>/<goal_id>/``:
  goal.yaml      FACTS    — objective, cadence, engine, workspace_dir, done_when, backlog
  STATUS.md      STATE    — a generated full-fidelity VIEW (since Tranche 1/PR3): the
                            source of truth is the ``goal_status`` SQLite table, but
                            every save still rewrites the whole STATUS.md (same YAML
                            frontmatter + body) so reverting PR3 recovers the state
  log.md         EVENTS   — append-only, newest at bottom
  inbox.md       STEERING — append-only direction (from Denys OR the evaluator); cursor-consumed
  deliveries.md  EVIDENCE — append-only, grounded record of what each action actually
                            shipped (agent summary + gate verdict + PR), read by the evaluator

Status is SQLite-backed via :class:`GoalState` (Tranche 1/PR3); the other
artifacts are still plain files, git-synced like the rest of the vault. A clock
is injected (``now``) so ticks are deterministic under test.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yaml

from .models import Goal, GoalStatus, InFlight
from .state import GoalState
from .transitions import (
    LEGAL,
    Event,
    IllegalTransition,
    State,
    TransitionConflict,
    derive_state,
)

if TYPE_CHECKING:
    from ..state_store import StateStore

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


class GoalDocCorrupt(RuntimeError):
    """A goal contract file EXISTS on disk but cannot be parsed.

    Distinct from "missing" on purpose: a missing checklist/firmed-draft is a
    legitimate state (backlog mode / pre-firming base goal), but a torn or
    garbled file means the goal's acceptance contract is GONE and nothing may
    silently fall back — a corrupt checklist used to read as "no checklist"
    and quietly flip the goal into the backlog planning pipeline; a corrupt
    firmed draft used to silently drop the firmed ``done_when`` /
    ``stub_acceptable`` / ``verify_cmd``. The tick catches this at one choke
    point and blocks loudly; display paths opt into graceful degrade via
    ``on_corrupt="none"``."""

    def __init__(self, goal_id: str, doc: str, cause: Exception) -> None:
        self.goal_id = goal_id
        self.doc = doc
        self.cause = cause
        super().__init__(f"{doc} for goal {goal_id!r} is corrupt: {cause}")


class GoalStore:
    def __init__(
        self,
        goals_dir: Path,
        *,
        now: Callable[[], datetime] = _default_now,
        state: "StateStore | None" = None,
    ) -> None:
        self._root = Path(goals_dir)
        self._now = now
        # Tranche 1 seam (substrate, UNUSED): the SQLite home goal state is being
        # consolidated into. When a shared StateStore is handed in (production
        # will pass the one that owns devclaw.db), we borrow it; otherwise we
        # self-create a private, isolated DB beside the goals so existing callers
        # — including ~40 GoalStore(tmp_path) test constructions — keep working
        # with zero changes. The GoalState bootstraps its tables but nothing
        # reads or writes them yet.
        if state is None:
            from ..state_store import StateStore

            state = StateStore(str(self._root / ".goal-state.db"))
        self._state = state
        self._goal_state = GoalState(self._state)

    # ---- discovery ---------------------------------------------------------

    def list_goal_ids(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if (p / "goal.yaml").is_file())

    def _dir(self, goal_id: str) -> Path:
        return self._root / goal_id

    def exists(self, goal_id: str) -> bool:
        return (self._dir(goal_id) / "goal.yaml").is_file()

    def _write_atomic(self, goal_id: str, name: str, text: str) -> None:
        """tmp-file + ``os.replace`` write for the goal's contract/artifact
        files. Same treatment ``save_status`` got after the 2026-07-09 live
        truncation (a crash mid-``write_text`` leaves a torn file that then
        fails to parse — or worse, parses as something else)."""
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{name}.tmp"
        tmp.write_text(text)
        os.replace(tmp, d / name)

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
        stub_acceptable: list[str] | None = None,
        skills_required: list[str] | None = None,
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
                    "stub_acceptable": list(stub_acceptable or []),
                    "skills_required": list(skills_required or []),
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
            stub_acceptable=[str(x).strip() for x in (raw.get("stub_acceptable") or []) if str(x).strip()],
            skills_required=[str(x).strip() for x in (raw.get("skills_required") or []) if str(x).strip()],
        )

    # ---- status (state) ----------------------------------------------------

    def load_status(self, goal_id: str) -> GoalStatus:
        # Source of truth is the goal_status table (Tranche 1/PR3). Migrate any
        # legacy STATUS.md into it lazily + idempotently, then read the row back.
        # A brand-new goal with neither a row nor a STATUS.md yields the default
        # status (unchanged from the file-only behavior) and writes nothing —
        # its first save_status creates the row.
        self._ensure_status_row(goal_id)
        if self._goal_state.has_status(goal_id):
            return self._goal_state.read_status(goal_id)
        return GoalStatus()

    def _ensure_status_row(self, goal_id: str) -> None:
        """Lazy, idempotent migration of a legacy STATUS.md into ``goal_status``.

        Row already present → no-op (the idempotency guard). No STATUS.md yet →
        no-op (brand-new/pre-save goal; ``load_status`` returns the default and
        the first ``save_status`` creates the row). A STATUS.md that EXISTS —
        even truncated/corrupt — is parsed with the current frontmatter reader
        (which degrades every field to its default, never raising: T0.4's
        GoalDocCorrupt is for checklist/firmed, NOT status) and INSERTed inside
        a ``transaction()``, seeding ``goal_phase_history`` from its current
        phase_history."""
        if self._goal_state.has_status(goal_id):
            return
        path = self._dir(goal_id) / "STATUS.md"
        if not path.exists():
            return
        parsed = self._parse_status_md(path.read_text())
        with self._state.transaction():
            # Re-check under the txn lock so a concurrent migrate can't
            # double-insert the row or double-seed phase_history.
            if self._goal_state.has_status(goal_id):
                return
            self._goal_state.write_status(goal_id, parsed)
            self._goal_state.seed_phase_history(goal_id, parsed.phase_history)

    @staticmethod
    def _parse_status_md(text: str) -> GoalStatus:
        """Parse a STATUS.md's YAML frontmatter into a GoalStatus. Used only by
        the lazy migration now; degrades field-by-field to defaults on a
        truncated/garbled file (``_read_frontmatter`` returns ``{}``) — never
        raises, matching the pre-PR3 ``load_status`` behavior exactly."""
        fm = GoalStore._read_frontmatter(text)
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
        raw_history = fm.get("phase_history") or []
        history: tuple[dict, ...] = tuple(
            {"phase": str(e.get("phase")), "at": str(e.get("at"))}
            for e in raw_history
            if isinstance(e, dict) and e.get("phase") and e.get("at")
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
            phase_history=history,
        )

    def save_status(self, goal_id: str, status: GoalStatus) -> None:
        # Source of truth is the goal_status table; STATUS.md is a generated
        # full-fidelity view rewritten on every save (the rollback path).
        #
        # Migrate any pre-existing STATUS.md history BEFORE appending, so a
        # first save on a goal that was never load_status()'d can't drop the
        # on-disk phase_history (idempotent — no-op once a row exists).
        self._ensure_status_row(goal_id)
        with self._state.transaction():
            # phase_history is append-only. The table is now authoritative, so
            # the old stale-snapshot merge hack (re-reading the disk file) is
            # gone: append a {phase, at} entry only when the phase actually
            # changed from what's stored.
            prev_phase = self._goal_state.current_phase(goal_id)
            if status.phase and status.phase != prev_phase:
                self._goal_state.append_phase_history(
                    goal_id, status.phase, self._now().isoformat(timespec="seconds")
                )
            history = self._goal_state.read_phase_history(goal_id)
            # PR4: stamp the derived enum state on EVERY write so the column
            # can never go stale relative to phase/lifecycle/in_flight. This
            # is still the UNGUARDED write path — no CAS, no legality check
            # (production transition sites use .transition() instead) — but
            # the column itself must always be correct so a later
            # .transition() call has a trustworthy `cur_state` to CAS from.
            status = replace(
                status, phase_history=history, state=derive_state(status).value,
            )
            self._goal_state.write_status(goal_id, status)
        # STATUS.md view — the exact frontmatter _read_frontmatter parses + the
        # human body, written via the atomic tmp+os.replace. This is the
        # rollback path: reverting PR3 makes load_status read this file again
        # and recover the current state (a crash mid-write, container restart —
        # 2026-07-09 — left a truncated file that must not orphan in-flight work).
        self._write_status_view(goal_id, status)

    def _load_status_for_cas(self, goal_id: str) -> GoalStatus:
        """The current row as a GoalStatus, or bare defaults when no row
        exists yet — the read side of transition()'s / force_block()'s CAS.
        Deliberately does NOT call :meth:`_ensure_status_row` itself (callers
        do that first, matching save_status's ordering) and does NOT fall
        back to STATUS.md — a status object built here only ever needs
        `.state`/`.version`, both of which are meaningless on a file that
        predates this table."""
        if self._goal_state.has_status(goal_id):
            return self._goal_state.read_status(goal_id)
        return GoalStatus()

    def transition(
        self, goal_id: str, event: "Event", new: GoalStatus, *, expect: GoalStatus,
    ) -> GoalStatus:
        """The choke point every PRODUCTION phase/lifecycle/in_flight change
        routes through (see :mod:`devclaw.goal.transitions`). Two guards, in
        order:

        1. **CAS** — the row's CURRENTLY STORED ``(state, version)`` must
           match ``expect``'s (or the fresh defaults, when no row exists yet).
           A mismatch means another writer (steer_goal / cancel_goal / a
           parallel tick) committed between the caller's load and this call;
           raises :class:`~devclaw.goal.transitions.TransitionConflict` and
           writes NOTHING — the caller's decision was based on a snapshot
           that's no longer current, so honoring it would silently clobber
           whatever landed in between (the stale-snapshot un-cancel class this
           PR closes).
        2. **Legality** — ``event`` must permit landing on ``derive_state(new)``
           from the row's CURRENT state per
           :data:`~devclaw.goal.transitions.LEGAL`. A miss raises
           :class:`~devclaw.goal.transitions.IllegalTransition` — always a
           bug, never an expected race.

        Only past both does this write (same shape as save_status: phase_history
        append when phase changed, then write_status, then the STATUS.md view
        AFTER the transaction commits). Returns the ACTUAL stored object
        (``new`` with ``state``/``version`` stamped) — callers MUST thread this
        forward instead of reusing their pre-call snapshot (see tick.py's
        "version threading rule").
        """
        self._ensure_status_row(goal_id)
        with self._state.transaction():
            fresh = self._load_status_for_cas(goal_id)
            cur_state = State(fresh.state) if fresh.state else derive_state(fresh)
            expect_state = State(expect.state) if expect.state else derive_state(expect)
            if cur_state != expect_state or fresh.version != expect.version:
                raise TransitionConflict(
                    goal_id,
                    expected=(expect_state, expect.version),
                    found=(cur_state, fresh.version),
                )
            target = derive_state(new)
            if target not in LEGAL.get((cur_state, event), frozenset()):
                raise IllegalTransition(goal_id, cur_state, event, target)
            prev_phase = self._goal_state.current_phase(goal_id)
            if new.phase and new.phase != prev_phase:
                self._goal_state.append_phase_history(
                    goal_id, new.phase, self._now().isoformat(timespec="seconds")
                )
            history = self._goal_state.read_phase_history(goal_id)
            written = replace(
                new, phase_history=history, state=target.value, version=fresh.version + 1,
            )
            self._goal_state.write_status(goal_id, written)
        self._write_status_view(goal_id, written)
        return written

    def update_status_fields(self, goal_id: str, **fields) -> GoalStatus:
        """Column-only telemetry update — ``last_tick_at`` / ``last_plan_at`` /
        ``last_progress_at`` / ``no_progress_notified`` / ``last_eval_verdict``
        / ``last_eval_at`` / ``last_eval_note`` / ``deliveries_since_eval``
        ONLY (see :data:`GoalState.STATUS_FIELD_COLUMNS`). NEVER a full-row
        rewrite, so it can never be the write that clobbers a concurrent
        phase/lifecycle/in_flight transition — this is the mechanism half of
        the fix .transition()'s CAS is the guard half of: bookkeeping writes
        (last-tick timestamps, eval verdicts) don't need to fight over the row
        at all when they physically cannot touch the columns a transition
        cares about. No CAS, by design — these fields never conflict with a
        concurrent transition.

        Raises ``ValueError`` on any key outside the allowed set (especially
        phase/lifecycle/in_flight/blocked_on/next — those MUST go through
        :meth:`transition`). Falls back to :meth:`save_status` when no row
        exists yet (first write for a goal). Returns the fresh, re-read
        ``GoalStatus``."""
        bad = set(fields) - set(GoalState.STATUS_FIELD_COLUMNS)
        if bad:
            raise ValueError(
                f"update_status_fields: disallowed field(s) {sorted(bad)} — only "
                f"{sorted(GoalState.STATUS_FIELD_COLUMNS)} may go through the "
                "column-only path; phase/lifecycle/in_flight/blocked_on/next "
                "must go through GoalStore.transition()"
            )
        self._ensure_status_row(goal_id)
        if not self._goal_state.has_status(goal_id):
            self.save_status(goal_id, replace(GoalStatus(), **fields))
            return self.load_status(goal_id)
        with self._state.transaction():
            self._goal_state.update_columns(goal_id, fields)
        fresh = self.load_status(goal_id)
        self._write_status_view(goal_id, fresh)
        return fresh

    def force_block(self, goal_id: str, blocked_on: str) -> bool:
        """Unconditional block write — bypasses the LEGAL-table check on
        purpose. This is the ESCAPE HATCH used ONLY by tick_goal's
        ``IllegalTransition`` catch: BLOCK is legal from every non-terminal
        state, so no matter what a handler was mid-way through when it
        proposed an illegal transition (always a bug, not an expected race —
        see :class:`~devclaw.goal.transitions.IllegalTransition`), the goal
        can always land on BLOCKED and the owner gets a legible ping instead
        of the tick loop crash-retrying forever.

        Preserves ``in_flight`` AS-IS (same reasoning as
        ``_block_on_corrupt_doc``: blocking stops new cognition, it must not
        orphan a running action). No-op — returns ``False``, writes nothing —
        when the goal is already DONE/CANCELLED (terminal; nothing calls this
        on a happy path, but a belt-and-suspenders guard against blocking a
        finished goal). Returns ``True`` when it wrote."""
        self._ensure_status_row(goal_id)
        with self._state.transaction():
            fresh = self._load_status_for_cas(goal_id)
            cur_state = State(fresh.state) if fresh.state else derive_state(fresh)
            if cur_state in (State.DONE, State.CANCELLED):
                return False
            new = replace(
                fresh, phase="blocked", lifecycle="executing", blocked_on=blocked_on, next="",
            )
            prev_phase = self._goal_state.current_phase(goal_id)
            if new.phase != prev_phase:
                self._goal_state.append_phase_history(
                    goal_id, new.phase, self._now().isoformat(timespec="seconds")
                )
            history = self._goal_state.read_phase_history(goal_id)
            written = replace(
                new, phase_history=history, state=State.BLOCKED.value, version=fresh.version + 1,
            )
            self._goal_state.write_status(goal_id, written)
        self._write_status_view(goal_id, written)
        return True

    def _write_status_view(self, goal_id: str, status: GoalStatus) -> None:
        """Render + atomically write the STATUS.md view for ``status``. Full
        fidelity: same frontmatter shape + body a reader/rollback needs."""
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
            "phase_history": [dict(e) for e in status.phase_history],
        }
        body = self._render_status_body(goal_id, status)
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n---\n\n" + body
        self._write_atomic(goal_id, "STATUS.md", text)

    # ---- log (events) ------------------------------------------------------

    def append_log(self, goal_id: str, message: str) -> None:
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "log.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — log\n\n")
        with path.open("a") as fh:
            fh.write(f"- [{self._now().isoformat(timespec='seconds')}] {message}\n")

    def log_contains(self, goal_id: str, needle: str) -> bool:
        """Whether the goal's full log mentions ``needle``. Used by the
        orphaned-program reconcile to tell "settled and recorded" apart from
        "the in-flight ref was lost before the result was ever seen"."""
        path = self._dir(goal_id) / "log.md"
        return path.exists() and needle in path.read_text()

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
        ts = self._now().isoformat(timespec="seconds")
        self._write_atomic(
            goal_id, "discovery.md",
            f"# {goal_id} — discovery brief\n\n_generated {ts}_\n\n{brief.strip()}\n",
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

        self._write_atomic(goal_id, "checklist.yaml", dump_checklist(checklist))

    def read_checklist(
        self, goal_id: str, *, on_corrupt: str = "raise"
    ) -> "Checklist | None":  # type: ignore[name-defined]
        """The current checklist, or ``None`` if the decomposer hasn't run
        yet (legacy goals + brand-new goals before the decomposing phase
        completes). The per-tick planner falls back to backlog-driven mode
        when this is ``None``.

        A file that EXISTS but fails to parse is a different animal: the
        goal's structured plan is torn, and treating it as absent would
        silently revert the goal to the backlog planning pipeline. Default is
        to raise :class:`GoalDocCorrupt` (the tick blocks loudly at its choke
        point); display paths pass ``on_corrupt="none"`` to degrade gracefully."""
        from .checklist import ChecklistParseError, parse_checklist

        path = self._dir(goal_id) / "checklist.yaml"
        if not path.exists():
            return None
        try:
            return parse_checklist(path.read_text())
        except ChecklistParseError as exc:
            if on_corrupt == "none":
                return None  # display-grade degrade — never for cognition/gating
            raise GoalDocCorrupt(goal_id, "checklist.yaml", exc) from exc

    # ---- firmed-draft (firming-phase output) -------------------------------

    def write_firmed_draft(self, goal_id: str, firmed: "FirmedGoal") -> None:  # type: ignore[name-defined]
        """Persist the firming-phase output as ``firmed-draft.yaml``. One file
        for both the in-progress (``status: needs_owner_answers``) and the
        ready-for-decomposer (``status: firmed``) states — git history is the
        audit log."""
        from .firmed import dump_firmed

        self._write_atomic(goal_id, "firmed-draft.yaml", dump_firmed(firmed))

    def read_firmed_draft(
        self, goal_id: str, *, on_corrupt: str = "raise"
    ) -> "FirmedGoal | None":  # type: ignore[name-defined]
        """The current firmed draft, or ``None`` if firming hasn't run yet
        (legacy goals + new goals before firming completes).

        A file that EXISTS but fails to parse is NOT "absent": treating it as
        such made :meth:`load_effective_goal` silently return the base goal —
        dropping the firmed ``done_when`` / ``stub_acceptable`` /
        ``verify_cmd`` acceptance contract with zero signal. Default raises
        :class:`GoalDocCorrupt`; display paths pass ``on_corrupt="none"``."""
        from .firmed import FirmedParseError, parse_firmed

        path = self._dir(goal_id) / "firmed-draft.yaml"
        if not path.exists():
            return None
        try:
            return parse_firmed(path.read_text())
        except FirmedParseError as exc:
            if on_corrupt == "none":
                return None  # display-grade degrade — never for cognition/gating
            raise GoalDocCorrupt(goal_id, "firmed-draft.yaml", exc) from exc

    def load_effective_goal(self, goal_id: str, *, on_corrupt: str = "raise") -> Goal:
        """The goal as it currently is, with firming's outputs overlaid on the
        original ``goal.yaml`` facts. Use this everywhere cognition + gating
        need the CURRENT effective state (decomposer, planner, evaluator,
        done-gate) — ``load_goal`` stays available for code that wants the
        owner's original statement (audit, history, the firming handler's own
        derived-goal builder).

        Only firmed-status drafts overlay. While firming is in flight (status
        ``needs_owner_answers``) the base goal is returned — the partial draft
        is not authoritative yet.

        ``on_corrupt`` is forwarded to :meth:`read_firmed_draft`: the default
        raises :class:`GoalDocCorrupt` on a torn draft (returning the base
        goal would silently drop the firmed acceptance contract); display
        paths pass ``"none"`` to fall back to the base goal gracefully."""
        base = self.load_goal(goal_id)
        firmed = self.read_firmed_draft(goal_id, on_corrupt=on_corrupt)
        if firmed is None or firmed.status != "firmed":
            return base
        from dataclasses import replace as _replace

        from .firmed import derive_done_when

        derived_done_when = derive_done_when(firmed) or base.done_when
        derived_stub_acceptable = (
            list(firmed.stub_acceptable) if firmed.stub_acceptable
            else list(base.stub_acceptable)
        )
        # verify_cmd: firming's value WINS when present. Closes the cf-11 churn
        # root cause — without this, a cascade can't update its own gate even
        # when firming derived a stricter contract (e.g. "gate runs pytest AND
        # playwright"), and the agent invents workarounds (Makefiles, pytest
        # wrappers) to smuggle new tools through the stale gate.
        derived_verify_cmd = firmed.verify_cmd or base.verify_cmd
        return _replace(
            base,
            done_when=derived_done_when,
            stub_acceptable=derived_stub_acceptable,
            verify_cmd=derived_verify_cmd,
        )

    # ---- scope spec (handed in by the waiter via create_goal) ---------------

    def write_spec(self, goal_id: str, spec: str) -> None:
        """Persist the agreed scope spec — what to build, what's out, constraints.
        Produced by the OpenClaw waiter's scope_grill conversation BEFORE the goal
        is created, passed in through create_goal, and read by the evaluator so
        done is judged against the shared contract."""
        ts = self._now().isoformat(timespec="seconds")
        self._write_atomic(
            goal_id, "spec.md", f"# {goal_id} — spec\n\n_agreed {ts}_\n\n{spec.strip()}\n"
        )

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
