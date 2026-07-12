"""Status — the single-writer / CAS choke point.

:class:`GoalStatusMixin` carries every phase/lifecycle/in_flight write:
``load_status`` and its lazy STATUS.md migration, the CAS-guarded
:meth:`GoalStatusMixin.transition` (the choke point every production
transition routes through), the column-only ``update_status_fields`` fast
path, ``force_block`` (the illegal-transition escape hatch), and the STATUS.md
view renderer.

Split out of ``GoalStore`` as a mixin on the SAME instance — every method here
runs against the ``self._state`` / ``self._goal_state`` / ``self._now`` /
``self._pending_mirrors`` the base ``GoalStore`` owns, so the transaction /
single-writer / mirror-deferral semantics are byte-identical to the pre-split
monolith.
"""

from __future__ import annotations

from dataclasses import replace

import yaml

from ..models import GoalStatus, InFlight
from ..state import GoalState
from ..transitions import (
    Event,
    IllegalTransition,
    State,
    TransitionConflict,
    derive_state,
)
from ...state_store import _now_ms
from .base import _FRONTMATTER


class GoalStatusMixin:
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
        fm = GoalStatusMixin._read_frontmatter(text)
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
        # Deferred (not written here) when this call is nested inside a
        # caller-opened transaction() — see _flush_or_defer_status_view.
        self._flush_or_defer_status_view(goal_id, status)

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
        consume_steering: "list[int] | None" = None,
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
        AFTER the transaction commits — or DEFERRED, when this call is itself
        nested inside a caller-opened ``transaction()`` (PR7's atomic dispatch/
        settle units): see :meth:`_flush_or_defer_status_view`). Returns the
        ACTUAL stored object (``new`` with ``state``/``version`` stamped) —
        callers MUST thread this forward instead of reusing their pre-call
        snapshot (see tick.py's "version threading rule").

        ``consume_steering`` (PR5): exact ``goal_steering`` row ids to mark
        consumed, INSIDE this same transaction, once past both guards. This
        is what makes "consume exactly the steering rows the planner just
        acted on" atomic with the decision write itself — a
        :class:`TransitionConflict`/:class:`IllegalTransition` raised above
        means this line never runs, so an abandoned tick's steering rides
        the rollback and stays unread (closes "steer-during-planner-await
        lost": the old model consumed by a count stamped AFTER the fact,
        which could sweep up a row the planner never saw).
        """
        # LEGAL is read off the package namespace (not the bare imported name)
        # so a test's ``monkeypatch.setattr(goal.store, "LEGAL", ...)`` — the
        # modeled-missing-edge regression — is honored exactly as it was when
        # ``transition`` lived in the monolith module the test patches.
        from devclaw.goal import store as _store_pkg

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
            if target not in _store_pkg.LEGAL.get((cur_state, event), frozenset()):
                raise IllegalTransition(goal_id, cur_state, event, target)
            if consume_steering:
                self._goal_state.consume_steering_rows(goal_id, consume_steering, _now_ms())
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
        self._flush_or_defer_status_view(goal_id, written)
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
        self._flush_or_defer_status_view(goal_id, fresh)
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
        self._flush_or_defer_status_view(goal_id, written)
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
