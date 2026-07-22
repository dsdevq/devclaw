"""The durable mind on disk — reusing the vault ``projects/`` convention.

Folded in from goalclaw. Layout per goal, under ``<goals_dir>/<goal_id>/``:
  goal.yaml           FACTS    — objective, cadence, engine, workspace_dir, done_when, backlog
  STATUS.md           STATE    — a generated full-fidelity VIEW (since Tranche 1/PR3): the
                                 source of truth is the ``goal_status`` SQLite table, but
                                 every save still rewrites the whole STATUS.md (same YAML
                                 frontmatter + body) so reverting PR3 recovers the state
  log.md              EVENTS   — a generated VIEW (since Tranche 1/PR6) mirroring the
                                 ``goal_log`` table, append-only, newest at bottom
  inbox.md            STEERING — append-only direction (from Denys OR the evaluator); human-readable
                                 mirror + hand-append input. The ``goal_steering`` SQLite table
                                 (since Tranche 1/PR5) is the source of truth for what's unread —
                                 consumed by exact row id, never by counting lines
  deliveries.md       EVIDENCE — a generated VIEW (since Tranche 1/PR6) mirroring the
                                 ``goal_deliveries`` table, append-only, grounded record of what
                                 each action actually shipped, read by the evaluator
  checklist.yaml       PLAN     — a generated VIEW (since Tranche 1/PR6) mirroring the
                                 ``goal_docs`` table (kind ``checklist``): the decomposer's
                                 structured plan, the source of truth the per-tick planner picks
                                 actions from
  firmed-draft.yaml    CONTRACT — a generated VIEW (since Tranche 1/PR6) mirroring the
                                 ``goal_docs`` table (kind ``firmed_draft``): the firming phase's
                                 done_when / stub_acceptable / verify_cmd acceptance contract

Status, steering, log, deliveries, and the checklist/firmed-draft docs are all
SQLite-backed via :class:`GoalState` (Tranche 1/PR3, PR5, PR6); ``spec.md`` /
``discovery.md`` are still plain files (display/prompt inputs, not
consumed-state). A clock is injected (``now``) so ticks are deterministic
under test.

The class was split into a package for legibility (behavior-preserving):

- this module (``base.py``) — the module regexes + ``parse_duration`` +
  ``_default_now``, and :class:`GoalStore` itself (construction, discovery,
  the transaction/mirror discipline, goal facts, clock helpers).
- :mod:`.status` — :class:`GoalStatusMixin`, the single-writer/CAS choke point
  (``load_status`` / ``transition`` / ``force_block`` / the STATUS.md view).
- :mod:`.content` — :class:`GoalContentMixin` + :class:`GoalDocCorrupt`, the
  log / settlements / deliveries / checklist / firmed-draft / inbox surfaces.

The mixins run on the same ``self._state`` / ``self._goal_state`` /
``self._pending_mirrors`` instance, so the transaction, single-writer, and
mirror-deferral semantics are byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yaml

from ..models import Goal, GoalStatus
from ..state import GoalState

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: settle-line arrow scan for the lazy settlement seed (PR7) — the token
#: immediately before " → " on a "- [ts] ... <token> → <status>" log line.
#: Deliberately loose (matches ANY " x → y" substring) so it over-captures
#: exactly what ``log_contains(f" {id} → ")`` used to answer True for —
#: readopt/sweep decisions must be IDENTICAL on legacy goals either way.
_SETTLE_ARROW_RE = re.compile(r" (\S+) → (\S+)")


def parse_duration(s: str) -> int:
    """'6h' / '1d' / '30m' / '90s' → seconds. Raises ValueError on garbage."""
    m = _DURATION.match(s or "")
    if not m:
        raise ValueError(f"bad cadence {s!r}; want <int><s|m|h|d>")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


# Mixin imports come AFTER the module constants above so ``status.py`` /
# ``content.py`` can ``from .base import _FRONTMATTER`` / ``_SETTLE_ARROW_RE``
# without hitting a not-yet-defined name during this module's import.
from .content import GoalContentMixin  # noqa: E402
from .status import GoalStatusMixin  # noqa: E402

if TYPE_CHECKING:
    from ...state_store import StateStore


class GoalStore(GoalStatusMixin, GoalContentMixin):
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
            from ...state_store import StateStore

            state = StateStore(str(self._root / ".goal-state.db"))
        self._state = state
        self._goal_state = GoalState(self._state)
        #: PR7 mirror discipline — file mirrors deferred by a mirror=False /
        #: render_view=False write (or a transition()/save_status() call
        #: that finds itself nested inside a caller-opened transaction())
        #: land here instead of hitting disk immediately. goal_id ->
        #: [("log", line) | ("delivery", block) | ("doc", filename, content)
        #: | ("status", GoalStatus), ...], in write order. Deliberately dumb
        #: (a plain dict of lists, no locking) — the tick is single-threaded
        #: per goal, so there's no concurrent writer to guard against here.
        #: See transaction()/render_mirrors()/discard_pending_mirrors().
        self._pending_mirrors: "dict[str, list]" = {}

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

    # ---- transactions + mirror discipline (Tranche 1/PR7) ------------------
    #
    # transaction() lets tick.py group several row writes spanning MULTIPLE
    # GoalStore calls (e.g. a dispatch's task/program creation + the status
    # transition + the log row) into ONE atomic unit — a crash or CAS
    # conflict anywhere inside rolls the WHOLE unit back. The rule this
    # implies: a FILE mirror (log.md / deliveries.md / checklist.yaml /
    # STATUS.md) must never be written while a transaction() opened here is
    # still open, because a rollback can still undo the DB row it would be
    # mirroring — a file written early would then show state the DB no
    # longer has (the emergency-downgrade rail depends on files always
    # matching the rolled-back DB). append_log/append_delivery/write_checklist
    # accept mirror=False/render_view=False for exactly this: skip the file
    # write and remember it in self._pending_mirrors instead.
    # transition()/save_status()/force_block()/update_status_fields() do the
    # SAME thing automatically for STATUS.md — they detect nesting via the
    # shared StateStore's transaction depth (see _flush_or_defer_status_view)
    # rather than taking their own mirror= parameter, since they're called
    # from dozens of standalone (non-nested) sites tick-wide and only the
    # NEW dispatch/settle sites ever nest them.

    def transaction(self):
        """Thin public passthrough to the shared StateStore's transaction().
        Nested transaction() calls (this one, plus whatever GoalStore method
        the caller invokes inside it) join the SAME atomic unit — one commit
        or one rollback at the outermost exit."""
        return self._state.transaction()

    def render_mirrors(self, goal_id: str) -> None:
        """Flush every mirror write deferred for ``goal_id`` (in the order
        recorded) to disk, then clear the pending list. Idempotent — a no-op
        when nothing is pending. Callers invoke this immediately AFTER their
        own transaction() block commits (see the dispatch/settle sites in
        tick.py); on the exception path they call discard_pending_mirrors
        instead, never this."""
        pending = self._pending_mirrors.pop(goal_id, None)
        if not pending:
            return
        for item in pending:
            kind = item[0]
            if kind == "log":
                _, line = item
                d = self._dir(goal_id)
                d.mkdir(parents=True, exist_ok=True)
                path = d / "log.md"
                if not path.exists():
                    path.write_text(f"# {goal_id} — log\n\n")
                with path.open("a") as fh:
                    fh.write(f"{line}\n")
            elif kind == "delivery":
                _, block = item
                d = self._dir(goal_id)
                d.mkdir(parents=True, exist_ok=True)
                path = d / "deliveries.md"
                if not path.exists():
                    path.write_text(f"# {goal_id} — deliveries (what each action shipped)\n\n")
                with path.open("a") as fh:
                    fh.write(block)
            elif kind == "doc":
                _, name, content = item
                self._write_atomic(goal_id, name, content)
            elif kind == "status":
                _, status = item
                self._write_status_view(goal_id, status)

    # ---- run-summary projection (goal close) ------------------------------

    def read_goal_traces(self, goal_id: str, *, kind: "str | None" = None,
                         limit: int = 1000) -> "list[dict]":
        """Read-only projection of the shared traces table for THIS goal —
        the run-summary's input (delivery events: gate/PR/diff stats). Pure
        SELECT through the same shared StateStore the transaction()
        passthrough uses; nothing here writes traces (PersistentTracer owns
        that), and the summary it feeds is a generated view, never read back
        for decisions."""
        return self._state.read_traces(goal_id=goal_id, kind=kind, limit=limit)

    def goal_trace_totals(self, goal_id: str) -> dict:
        """Aggregate cognition token/cost totals for the run summary —
        :meth:`StateStore.trace_totals` passthrough (cheap SQL, no LLM)."""
        return self._state.trace_totals(goal_id=goal_id)

    def write_run_summary_view(self, goal_id: str, text: str) -> None:
        """RUN_SUMMARY.md — the at-a-glance close-out artifact, written once
        at the ACHIEVE close. A generated VIEW like STATUS.md/log.md/
        deliveries.md: a projection of rows (traces + phase_history +
        checklist), human- and rollback-readable, never read back for
        decisions. Atomic write (same torn-file treatment as the other
        views). Called AFTER the ACHIEVE transition commits — never inside a
        transaction() — so a rolled-back close can't leave a summary for a
        goal that didn't actually close. The assert makes that contract
        mechanical: a future in-transaction caller fails loudly here instead
        of silently violating the mirror discipline."""
        assert self._state._txn_depth == 0, (
            "write_run_summary_view must not run inside a transaction() — "
            "a rollback could leave a summary for a close that never happened"
        )
        self._write_atomic(goal_id, "RUN_SUMMARY.md", text)

    def discard_pending_mirrors(self, goal_id: str) -> None:
        """Drop any mirror writes deferred for ``goal_id`` WITHOUT rendering
        them — the exception-path counterpart of render_mirrors(), called
        when the transaction() they belonged to rolled back. Prevents an
        abandoned tick's mirror lines from leaking into the NEXT successful
        flush for the same goal."""
        self._pending_mirrors.pop(goal_id, None)

    def _flush_or_defer_status_view(self, goal_id: str, status: GoalStatus) -> None:
        """Render STATUS.md now, UNLESS this write is nested inside a
        caller-opened transaction() (the atomic dispatch/settle units): the
        DB write hasn't actually committed at that point (StateStore joins
        nested transaction() calls into one outer commit), so rendering the
        file immediately would show state a rollback could still undo.
        Deferred writes join the SAME pending-mirror list append_log /
        append_delivery / write_checklist use — the caller's render_mirrors()
        (called right after ITS OWN transaction() exits) flushes it, or
        discard_pending_mirrors() drops it on the exception path. Standalone
        (non-nested) callers — the overwhelming majority of call sites —
        behave exactly as before: depth is 0, so this renders immediately."""
        if self._state._txn_depth > 0:
            self._pending_mirrors.setdefault(goal_id, []).append(("status", status))
        else:
            self._write_status_view(goal_id, status)

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
        mode: str = "long_lived",
        strictness: str = "trust",
    ) -> Goal:
        """Write a new goal.yaml. Raises FileExistsError if the id is taken."""
        if self.exists(goal_id):
            raise FileExistsError(f"goal {goal_id!r} already exists")
        # Fail LOUD at creation, not silently every tick. cadence is read by
        # cadence_due() on every heartbeat via parse_duration(); an unparseable
        # value like "urgent" otherwise writes fine here and then throws an
        # isolated tick error every ~15min forever, wedging the goal with no
        # actionable surface (fs-monitoring-outage-refile-2026-07-19 died this
        # way — born with cadence "urgent", never once ticked).
        try:
            parse_duration(cadence)
        except ValueError as e:
            raise ValueError(
                f"cannot create goal {goal_id!r}: {e} — cadence must be a "
                f"duration like '15m', '6h', or '1d', not a word"
            ) from e
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
                    "mode": mode,
                    "strictness": strictness,
                },
                sort_keys=False,
            )
        )
        return self.load_goal(goal_id)

    def load_goal(self, goal_id: str) -> Goal:
        # NOTE: unknown keys in goal.yaml are ignored by construction — every
        # field is read explicitly below. Legacy files written before a field
        # was removed (e.g. the retired ``skills_required``) must keep loading.
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
            # Anything unrecognized (or a legacy file with no field) reads as
            # long_lived — the conservative default: the per-tick loop.
            mode=("one_shot" if raw.get("mode") == "one_shot" else "long_lived"),
            # Legacy / unrecognized reads as "trust" (advisory) — the default
            # dial: dial-able gates log-and-ship rather than wedge (ADR 0007).
            strictness=("strict" if raw.get("strictness") == "strict" else "trust"),
        )

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
