"""Content surfaces — everything the goal accretes that ISN'T the status row.

:class:`GoalContentMixin` carries the log, settlements, deliveries, checklist,
firmed-draft, discovery/spec, and inbox/steering surfaces — each row-first
(SQLite is the source of truth since Tranche 1/PR5–PR7) with the ``.md`` files
as generated / hand-append mirrors, plus the lazy one-shot migrations that seed
those rows from legacy on-disk files. :class:`GoalDocCorrupt` (the loud
fail-closed on a torn acceptance contract) lives here too.

Split out of ``GoalStore`` as a mixin on the SAME instance — every method here
runs against the ``self._state`` / ``self._goal_state`` / ``self._now`` /
``self._dir`` / ``self._pending_mirrors`` the base ``GoalStore`` owns, so the
row-first ordering, mirror-deferral, and lazy-ingest semantics are byte-identical
to the pre-split monolith.
"""

from __future__ import annotations

import re

from ..models import Goal, GoalStatus
from ...state_store import _now_ms
from .base import _SETTLE_ARROW_RE


class GoalDocCorrupt(RuntimeError):
    """A goal's acceptance-contract document exists but cannot be parsed.

    Distinct from "missing" on purpose: a missing checklist/firmed-draft is a
    legitimate state (backlog mode / pre-firming base goal), but a torn or
    garbled doc means the goal's acceptance contract is GONE and nothing may
    silently fall back — a corrupt checklist used to read as "no checklist"
    and quietly flip the goal into the backlog planning pipeline; a corrupt
    firmed draft used to silently drop the firmed ``done_when`` /
    ``stub_acceptable`` / ``verify_cmd``. The tick catches this at one choke
    point and blocks loudly; display paths opt into graceful degrade via
    ``on_corrupt="none"``.

    Since Tranche 1/PR6, ``checklist``/``firmed_draft`` live in the
    ``goal_docs`` table. A LEGACY goal (no DB row yet) can still have a torn
    ``checklist.yaml``/``firmed-draft.yaml`` on disk, and this exception
    still fires for it exactly as before — the file is never ingested, so
    the corruption isn't laundered into the DB by a later read. Once a goal
    HAS a row, SQLite's atomic upsert makes a new torn write structurally
    impossible; this class then only fires on a "should be impossible"
    DB-content parse failure, which still raises rather than silently
    downgrading (see ``read_checklist``/``read_firmed_draft``)."""

    def __init__(self, goal_id: str, doc: str, cause: Exception) -> None:
        self.goal_id = goal_id
        self.doc = doc
        self.cause = cause
        super().__init__(f"{doc} for goal {goal_id!r} is corrupt: {cause}")


class GoalContentMixin:
    # ---- log (events) — PR6: goal_log rows are the source of truth --------
    #
    # log.md is a pure OUTPUT view — nothing hand-appends to it (unlike
    # inbox.md) — so migration is a true one-shot: :meth:`_ingest_log` runs
    # its check on every call but only ever DOES anything once per goal
    # (guarded by ``has_log_rows``).

    def _ingest_log(self, goal_id: str) -> None:
        """Lazy, one-shot migration of a legacy log.md into ``goal_log``
        rows. Zero rows AND log.md exists → every line starting with
        ``- [`` (the same filter the pre-PR6 ``recent_log`` used) is
        inserted verbatim, in file order. No cursor needed: once ANY row
        exists for the goal this is a no-op forever (``has_log_rows``)."""
        if self._goal_state.has_log_rows(goal_id):
            return
        path = self._dir(goal_id) / "log.md"
        if not path.exists():
            return
        lines = [ln for ln in path.read_text().splitlines() if ln.startswith("- [")]
        if not lines:
            return
        self._goal_state.append_log_rows(goal_id, lines, _now_ms())

    def append_log(self, goal_id: str, message: str, *, mirror: bool = True) -> None:
        """Append one log line. Row-first, then the log.md mirror — the
        OPPOSITE order from ``append_steering``'s file-first, and
        deliberately so: inbox.md is a hand-append INPUT that self-heals via
        re-ingestion on the next read, so PR5 protected against losing a
        steering line by writing the file first. log.md is a pure OUTPUT
        view with no re-ingestion once a goal has rows — a mirror line
        without a row would be silently invisible to every DECISION reader
        (``recent_log``) forever, while a row without a
        mirror line is merely a cosmetically stale (but harmless) log.md
        after a crash between the two writes. Rows are truth, so the row
        write must never be the one left dangling.

        ``mirror=False`` (PR7): skip the file append and remember the
        rendered line in ``self._pending_mirrors[goal_id]`` instead — for
        callers writing INSIDE an open ``transaction()`` (the atomic
        dispatch/settle units), where a file write must never race a
        possible rollback. The caller flushes via ``render_mirrors()`` after
        its transaction commits, or drops via ``discard_pending_mirrors()``
        on the exception path."""
        self._ingest_log(goal_id)
        line = f"- [{self._now().isoformat(timespec='seconds')}] {message}"
        self._goal_state.append_log_row(goal_id, line, _now_ms())
        if not mirror:
            self._pending_mirrors.setdefault(goal_id, []).append(("log", line))
            return
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "log.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — log\n\n")
        with path.open("a") as fh:
            fh.write(f"{line}\n")

    def recent_log(self, goal_id: str, n: int = 20) -> str:
        """The last ``n`` log lines, newline-joined, oldest-of-the-tail
        first — byte-identical to the pre-PR6 file-tail read for both fresh
        and migrated goals."""
        self._ingest_log(goal_id)
        return "\n".join(self._goal_state.recent_log_rows(goal_id, n))

    # ---- settlements (settled-and-recorded truth — PR7) --------------------
    #
    # goal_settlements has no corresponding .md view — these are plain row
    # writes/reads. record_settlement joins whichever transaction() (if any)
    # is open; is_settled lazy-seeds from historical goal_log rows first so
    # a goal that settled work before PR7 ever existed answers identically
    # to the old ``log_contains(f" {id} → ")`` guard.

    def record_settlement(
        self, goal_id: str, *, ref_id: str, ref_kind: "str | None", status: "str | None",
    ) -> bool:
        """Record ONE settled ref. INSERT OR IGNORE against
        ``UNIQUE(goal_id, ref_id)`` — a settle retried after a
        ``TransitionConflict`` rollback re-records the identical row, no
        duplicate. Row-only; no file mirror to defer."""
        return self._goal_state.record_settlement(goal_id, ref_id, ref_kind, status, _now_ms())

    def is_settled(self, goal_id: str, ref_id: str) -> bool:
        """Whether ``ref_id`` has a recorded settlement for ``goal_id`` — the
        orphan sweep's "settled and recorded" vs. "lost mid-flight" guard.
        Lazy-seeds from historical log rows first (see
        :meth:`_seed_settlements`) so legacy goals answer identically to the
        pre-PR7 ``log_contains(f" {id} → ")`` check."""
        self._seed_settlements(goal_id)
        return self._goal_state.has_settlement(goal_id, ref_id)

    def _seed_settlements(self, goal_id: str) -> None:
        """One-shot lazy seed of ``goal_settlements`` from historical
        ``goal_log`` rows — the migration path for a goal that settled work
        before ``goal_settlements`` was ever read. Guarded on ZERO existing
        settlement rows for the goal (real or seeded) — never re-seeds once
        anything has been recorded.

        Intentionally over-captures: :data:`_SETTLE_ARROW_RE` matches ANY
        ``... <token> → <status>`` substring on a log line and seeds a
        settlement for ``<token>``, exactly matching what
        ``log_contains(f" {id} → ")`` used to answer True for — so
        readopt/sweep decisions are IDENTICAL on legacy goals either way. A
        fresh goal (no log content at all) seeds nothing; the guard stays
        open until its first REAL settlement row."""
        if self._goal_state.has_any_settlements(goal_id):
            return
        self._ingest_log(goal_id)  # legacy log.md → rows first, so the scan is complete
        rows = self._goal_state.all_log_rows(goal_id)
        if not rows:
            return
        now = _now_ms()
        for line in rows:
            m = _SETTLE_ARROW_RE.search(line)
            if not m:
                continue
            ref_id, status = m.group(1), m.group(2)
            self._goal_state.record_settlement(goal_id, ref_id, None, status, now)

    # ---- deliveries (grounded evidence for the evaluator) — PR6: rows are
    # the source of truth, deliveries.md the mirror. Same shape as log.

    def _ingest_deliveries(self, goal_id: str) -> None:
        """Lazy, one-shot migration of a legacy deliveries.md into
        ``goal_deliveries`` rows. Zero rows AND the file exists → split the
        body (everything from the first ``## [`` section header onward —
        the fixed ``# … — deliveries`` header line is discarded, since
        ``recent_deliveries`` reconstructs it from the known constant
        format) into sections, one row per section: ``ref_id`` NULL (legacy
        sections predate the idempotency key and have nothing to dedupe
        against), ``instruction`` parsed off the section's head line,
        ``body`` the section's FULL text verbatim including its trailing
        blank-line separation — so ``"".join(blocks)`` reconstructs the
        original byte-for-byte. Guarded like ``_ingest_log``."""
        if self._goal_state.has_delivery_rows(goal_id):
            return
        path = self._dir(goal_id) / "deliveries.md"
        if not path.exists():
            return
        sections = self._split_delivery_sections(path.read_text())
        if not sections:
            return
        ts_ms = _now_ms()
        with self._state.transaction():
            for instruction, block in sections:
                self._goal_state.append_delivery_row(
                    goal_id, None, block, ts_ms, instruction=instruction,
                )

    _DELIVERY_HEAD = re.compile(r"^## \[", re.MULTILINE)
    _DELIVERY_HEAD_LINE = re.compile(r"^## \[[^\]]*\]\s*(.*)$")

    @classmethod
    def _split_delivery_sections(cls, text: str) -> "list[tuple[str, str]]":
        """Split a deliveries.md body into ``(instruction, block)`` pairs on
        lines starting ``## [`` — the exact boundary ``append_delivery``
        writes. Text before the first match (the header) is dropped on
        purpose; each returned ``block`` runs from its ``## [`` line up to
        (not including) the next one, or end of text for the last section."""
        starts = [m.start() for m in cls._DELIVERY_HEAD.finditer(text)]
        sections: list[tuple[str, str]] = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            block = text[start:end]
            head_line = block.splitlines()[0] if block else ""
            m = cls._DELIVERY_HEAD_LINE.match(head_line)
            instruction = m.group(1) if m else ""
            sections.append((instruction, block))
        return sections

    def append_delivery(
        self, goal_id: str, instruction: str, body: str, *,
        ref_id: "str | None" = None, mirror: bool = True,
    ) -> None:
        """Append a grounded record of what one action actually shipped — the
        agent's own summary + the gate verdict + the PR url, captured in-process
        from the full task row (not the old over-the-wire blob). This is the
        substrate the direction evaluator reads to judge shipped-vs-correct.

        ``ref_id`` (PR6) is the in-flight ref's id, threaded through by the
        settle call site so a duplicate settle of the SAME ref (e.g. a
        ``TransitionConflict`` retry landing after the first settle already
        recorded the delivery) is a no-op: no second row, no second section
        in deliveries.md. ``None`` (the default — callers that never settle
        against a ref, e.g. tests) always inserts, matching pre-PR6
        behavior exactly. Row-first, then the file mirror, ONLY when a row
        was actually inserted — a duplicate ref_id must never produce a
        duplicate section in the view (see ``GoalState.append_delivery_row``).

        ``mirror=False`` (PR7): once a row IS inserted, skip the file append
        and remember the rendered section in ``self._pending_mirrors`` —
        same deferral contract as :meth:`append_log`, for callers writing
        inside an open ``transaction()``."""
        self._ingest_deliveries(goal_id)
        ts = self._now().isoformat(timespec="seconds")
        block = f"## [{ts}] {instruction}\n\n{body.strip()}\n\n"
        inserted = self._goal_state.append_delivery_row(
            goal_id, ref_id, block, _now_ms(), instruction=instruction,
        )
        if not inserted:
            return  # duplicate ref_id — silent idempotency is the point
        if not mirror:
            self._pending_mirrors.setdefault(goal_id, []).append(("delivery", block))
            return
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "deliveries.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — deliveries (what each action shipped)\n\n")
        with path.open("a") as fh:
            fh.write(block)

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
    # PR6: goal_docs (kind "checklist") is the source of truth; checklist.yaml
    # is a generated view, same shape as STATUS.md/log.md/deliveries.md.

    def write_checklist(
        self, goal_id: str, checklist: "Checklist", *, render_view: bool = True,  # type: ignore[name-defined]
    ) -> None:
        """Persist the decomposer's full output. Writes the ``goal_docs`` row
        FIRST (the source of truth the per-tick planner picks actions from;
        mutable across ticks — settle hook + steer can rewrite items), then
        the ``checklist.yaml`` view via the same atomic tmp+os.replace
        treatment ``STATUS.md`` gets — the rollback path, and a legible
        artifact to read without a DB client.

        ``render_view=False`` (PR7): skip the file write and remember the
        rendered content in ``self._pending_mirrors`` — same deferral
        contract as :meth:`append_log`, for callers (the dispatch hook's
        in_flight flag, a settle's checklist update) writing inside an open
        ``transaction()``."""
        from ..checklist import dump_checklist

        content = dump_checklist(checklist)
        self._goal_state.write_doc(goal_id, "checklist", content, _now_ms())
        if not render_view:
            self._pending_mirrors.setdefault(goal_id, []).append(("doc", "checklist.yaml", content))
            return
        self._write_atomic(goal_id, "checklist.yaml", content)

    def read_checklist(
        self, goal_id: str, *, on_corrupt: str = "raise"
    ) -> "Checklist | None":  # type: ignore[name-defined]
        """The current checklist, or ``None`` if the decomposer hasn't run
        yet (legacy goals + brand-new goals before the decomposing phase
        completes). The per-tick planner falls back to backlog-driven mode
        when this is ``None``.

        DB-first: a ``goal_docs`` row exists → parse it. A parse failure on
        DB content still raises :class:`GoalDocCorrupt` — SQLite's atomic
        upsert makes a torn ROW structurally impossible, so this branch
        should be unreachable, but "should be impossible" is not a license
        to silently downgrade (fail loud, per T0.4).

        No row → LEGACY file path (a goal that predates PR6, or one where
        the decomposer genuinely hasn't run): file absent → ``None``
        (legitimate); file present but corrupt → the EXACT pre-PR6 behavior
        (:class:`GoalDocCorrupt`, or ``None`` for ``on_corrupt="none"``) —
        the corrupt file is NEVER ingested into the DB, so a torn contract
        can't be laundered into "migrated" truth. A file that parses cleanly
        IS migrated (the row is written with its content verbatim) so this
        goal never takes the legacy path again."""
        from ..checklist import ChecklistParseError, parse_checklist

        content = self._goal_state.read_doc(goal_id, "checklist")
        if content is not None:
            try:
                return parse_checklist(content)
            except ChecklistParseError as exc:
                # Honor the T0.4 split even on this should-be-impossible
                # branch: cognition/gating raises, display degrades.
                if on_corrupt == "none":
                    return None
                raise GoalDocCorrupt(goal_id, "checklist.yaml", exc) from exc
        path = self._dir(goal_id) / "checklist.yaml"
        if not path.exists():
            return None
        text = path.read_text()
        try:
            parsed = parse_checklist(text)
        except ChecklistParseError as exc:
            if on_corrupt == "none":
                return None  # display-grade degrade — never for cognition/gating
            raise GoalDocCorrupt(goal_id, "checklist.yaml", exc) from exc
        self._goal_state.write_doc(goal_id, "checklist", text, _now_ms())  # migrate
        return parsed

    # ---- firmed-draft (firming-phase output) -------------------------------
    # PR6: goal_docs (kind "firmed_draft") is the source of truth;
    # firmed-draft.yaml is a generated view — same DB-first/legacy-fallback
    # shape as read_checklist above.

    def write_firmed_draft(self, goal_id: str, firmed: "FirmedGoal") -> None:  # type: ignore[name-defined]
        """Persist the firming-phase output. One doc for both the in-progress
        (``status: needs_owner_answers``) and the ready-for-decomposer
        (``status: firmed``) states — the ``goal_docs`` row's history (and
        git history on the ``firmed-draft.yaml`` view) is the audit log."""
        from ..firmed import dump_firmed

        content = dump_firmed(firmed)
        self._goal_state.write_doc(goal_id, "firmed_draft", content, _now_ms())
        self._write_atomic(goal_id, "firmed-draft.yaml", content)

    def read_firmed_draft(
        self, goal_id: str, *, on_corrupt: str = "raise"
    ) -> "FirmedGoal | None":  # type: ignore[name-defined]
        """The current firmed draft, or ``None`` if firming hasn't run yet
        (legacy goals + new goals before firming completes).

        DB-first, same shape as :meth:`read_checklist`: a row's parse
        failure still raises (should be impossible post-migration — SQLite's
        atomic upsert kills the torn-write class — but never silently
        downgrades). No row → legacy file path: absent → ``None``; present
        but corrupt → NOT "absent" — treating it as such made
        :meth:`load_effective_goal` silently return the base goal, dropping
        the firmed ``done_when`` / ``stub_acceptable`` / ``verify_cmd``
        acceptance contract with zero signal — so this raises
        :class:`GoalDocCorrupt` (or degrades to ``None`` for
        ``on_corrupt="none"``) and is NEVER ingested; a clean parse migrates
        the row verbatim."""
        from ..firmed import FirmedParseError, parse_firmed

        content = self._goal_state.read_doc(goal_id, "firmed_draft")
        if content is not None:
            try:
                return parse_firmed(content)
            except FirmedParseError as exc:
                # Honor the T0.4 split even on this should-be-impossible
                # branch: cognition/gating raises, display degrades.
                if on_corrupt == "none":
                    return None
                raise GoalDocCorrupt(goal_id, "firmed-draft.yaml", exc) from exc
        path = self._dir(goal_id) / "firmed-draft.yaml"
        if not path.exists():
            return None
        text = path.read_text()
        try:
            parsed = parse_firmed(text)
        except FirmedParseError as exc:
            if on_corrupt == "none":
                return None  # display-grade degrade — never for cognition/gating
            raise GoalDocCorrupt(goal_id, "firmed-draft.yaml", exc) from exc
        self._goal_state.write_doc(goal_id, "firmed_draft", text, _now_ms())  # migrate
        return parsed

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

        from ..firmed import derive_done_when

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
        """The tail of the deliveries record (bounded — the evaluator's
        grounding context). Reconstructs ``header + "".join(blocks)`` from
        ``goal_deliveries`` rows — byte-identical to the pre-PR6
        ``deliveries.md`` file-tail read, since the header format
        (``# {goal_id} — deliveries (what each action shipped)\\n\\n``) is
        the one constant :meth:`append_delivery` has ever written."""
        self._ingest_deliveries(goal_id)
        blocks = self._goal_state.recent_delivery_blocks(goal_id)
        if not blocks:
            return ""
        text = f"# {goal_id} — deliveries (what each action shipped)\n\n" + "".join(blocks)
        return text[-chars:] if len(text) > chars else text

    # ---- inbox (steering) — PR5: goal_steering rows are the source of truth
    #
    # ``inbox.md`` stays BOTH the human-readable mirror (every machine append
    # writes a row AND a matching line) AND a hand-append INPUT (a line typed
    # straight into the file is lazily ingested into a row the next time
    # anything reads steering — see ``_ingest_inbox``). Consumption ("the
    # planner acted on this") is by row id, via ``GoalStore.transition``'s
    # ``consume_steering=``, never by counting lines — that count-based model
    # is exactly what let a steer landing during the planner's cognition
    # await get silently swallowed (steer-during-planner-await lost).

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

    def _ingest_inbox(self, goal_id: str) -> None:
        """Lazily convert ``inbox.md`` lines the store doesn't have
        ``goal_steering`` rows for yet into rows. Called before every
        steering read (``unread_steering_rows`` / ``unread_steering``) so a
        line typed straight into the file — or mirrored there by
        ``append_steering`` — becomes (or stays) visible without ever being
        double-counted.

        ``goal_status.inbox_ingest_cursor`` is this method's OWN boundary —
        "how many inbox.md lines have already been turned into rows" — a
        DIFFERENT thing from "how many are consumed". Only lines PAST the
        cursor are new; already-ingested lines (including everything
        ``append_steering`` just mirrored, which advances the cursor itself)
        are never re-ingested. A no-op — no write, no version bump — when
        there is nothing new AND no migration is due, so a normal tick that
        finds no hand-typed content pays zero SQL writes for this call.

        Lazy migration (first ingest for a goal that pre-dates PR5): before
        this PR, the stored cursor WAS the consume cursor — lines below it
        are already-acted-on history, not fresh steering. The FIRST ingest
        for a goal with ZERO existing ``goal_steering`` rows treats
        ``lines[:cursor]`` as already-CONSUMED (preserved for the record,
        never fed to the planner) and only ``lines[cursor:]`` as new.
        Idempotent by construction: once ANY row exists for the goal, this
        branch can never fire again.

        Tolerates ``cursor > len(lines)`` (an operator clearing/truncating
        ``inbox.md`` by hand, or a crash between a row+cursor commit and the
        file catching up to it): ``lines[cursor:]`` is then simply empty —
        nothing is ingested, nothing goes negative, and the cursor is left
        alone until the file has genuinely new content past it."""
        self._ensure_status_row(goal_id)
        if not self._goal_state.has_status(goal_id):
            # Brand-new goal — no STATUS.md to migrate (_ensure_status_row is
            # a no-op for one) and no row yet either. Give
            # set_inbox_ingest_cursor somewhere to write.
            self.save_status(goal_id, GoalStatus())
        with self._state.transaction():
            lines = self._inbox_lines(goal_id)
            cursor = self._goal_state.read_status(goal_id).inbox_cursor
            new_lines = lines[cursor:]
            first_ingest = not self._goal_state.has_steering_rows(goal_id)
            migrate_history = first_ingest and cursor > 0 and lines[:cursor]
            if migrate_history:
                now = _now_ms()
                self._goal_state.append_steering_rows(
                    goal_id, lines[:cursor], source="manual",
                    created_at_ms=now, consumed=True,
                )
            if new_lines:
                self._goal_state.append_steering_rows(goal_id, new_lines, source="manual")
            if new_lines or migrate_history:
                self._goal_state.set_inbox_ingest_cursor(goal_id, len(lines))

    def unread_steering_rows(self, goal_id: str) -> "list[tuple[int, str]]":
        """Unread steering — the exact-id source of truth PR5 introduced.
        Ingests any new hand-typed ``inbox.md`` lines into rows FIRST (lazy,
        idempotent — see ``_ingest_inbox``), then returns ``[(id, line), ...]``
        for every row with ``consumed_at IS NULL``, oldest first. Callers that
        need to consume EXACTLY what they read (the tick's post-plan
        transition) thread the ids into ``GoalStore.transition(...,
        consume_steering=[...])`` — that call, not this read, is what marks
        them consumed."""
        self._ingest_inbox(goal_id)
        rows = self._goal_state.unread_steering_rows(goal_id)
        return [(r["id"], r["line"]) for r in rows]

    def unread_steering(self, goal_id: str) -> str:
        """Unread steering as one newline-joined string — kept for display /
        back-compat callers that don't consume by exact id. Re-implemented on
        top of :meth:`unread_steering_rows` (the row-backed source of truth
        PR5 introduced). Consumption is by exact row id via
        ``GoalStore.transition(consume_steering=...)``, never by a cursor —
        this read-only helper has nothing to do with that; it never took a
        ``status`` argument to consume from (PR8 dropped the long-dead
        parameter)."""
        return "\n".join(line for _, line in self.unread_steering_rows(goal_id)).strip()

    def append_steering(self, goal_id: str, lines: list[str], *, source: str = "denys") -> None:
        """Append steering lines. Writes UNCONSUMED ``goal_steering`` rows
        (the source of truth the planner reads) AND mirrors the same lines
        into ``inbox.md`` in the historical ``- [{source} {ts}] {line}``
        format — kept EXACTLY, so ``devclaw.trend_signals``' H4 signal, which
        parses that prefix straight off the file, keeps working unchanged.

        The row stores the SAME formatted line as the file, not the bare
        text: the row's ``line`` is what ``unread_steering`` feeds the
        planner, and the planner prompt (``prompts/goal-planner.md``)
        documents evaluator corrections as "marked [auto-eval]" — that
        marker must survive the move to row-backed steering, byte-identical
        to what the pre-PR5 file read produced. It also keeps machine rows
        and hand-ingested rows consistent (both hold the inbox.md line
        verbatim); the structured ``source`` column exists separately for
        queries.

        Runs ``_ingest_inbox`` FIRST so any pre-existing hand-typed
        ``inbox.md`` lines get their own rows (and the ingest cursor catches
        up to them) BEFORE this call's own cursor math — otherwise a
        hand-typed line sitting between the old cursor and the file's end
        would be silently skipped once this call moves the cursor past it.

        Ordering — deliberately FILE-append first, THEN the rows+cursor
        commit: a crash in between leaves ``inbox.md`` with a line the rows
        don't know about yet, which the next ``_ingest_inbox`` call picks up
        as an ordinary hand-typed ``manual``-sourced row — the source label
        is wrong but nothing is LOST. The reverse order (rows first) risks
        the opposite: a crash after the row+cursor commit but before the
        file write leaves the cursor ahead of the file, and if the retried
        file write never lands with the exact same content, that text can
        end up permanently below the (already-advanced) cursor — a genuine
        loss. Losing steering is worse than a rare re-sourced duplicate, so
        file-first wins.

        The cursor is set to the CURRENT total ``inbox.md`` line count (read
        fresh, after our own append) rather than computed as "old cursor +
        len(clean)" — self-correcting regardless of exactly what
        ``_ingest_inbox`` left it at."""
        clean = [ln.strip() for ln in lines if ln.strip()]
        if not clean:
            return
        self._ingest_inbox(goal_id)
        d = self._dir(goal_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "inbox.md"
        if not path.exists():
            path.write_text(f"# {goal_id} — inbox (steering)\n\n")
        ts = self._now().isoformat(timespec="seconds")
        formatted = [f"- [{source} {ts}] {ln}" for ln in clean]
        with path.open("a") as fh:
            for ln in formatted:
                fh.write(f"{ln}\n")
        with self._state.transaction():
            self._goal_state.append_steering_rows(goal_id, formatted, source=source)
            self._goal_state.set_inbox_ingest_cursor(goal_id, len(self._inbox_lines(goal_id)))
