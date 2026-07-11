"""The firming phase — the first concrete :class:`PhaseHandler`.

Turns a rough goal + research into a structurally complete goal the decomposer
can plan against. Two entry points:

  * :meth:`FirmingHandler.run` — tick calls this when lifecycle is
    ``firming`` and there's no in-flight work. Runs round-1 cognition,
    persists the draft, either advances to ``executing`` (no unknowns)
    or blocks with an OWNER ping (unknowns surfaced).

  * :meth:`FirmingHandler.handle_answer` — the ``answer_unknowns`` MCP
    tool calls this when the owner has answered every current unknown.
    Runs round N+1 cognition, persists the new draft, returns the
    structured result the waiter renders.

The cognition is a SINGLE prompt (`devclaw/prompts/firming.md`) that
handles both rounds; round number is a parameter. Opus tier — getting
unknowns wrong wastes downstream cost and owner attention. See
``~/memory/projects/devclaw/proposals/2026-06-27-firming-phase.md``."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING, Awaitable, Callable

from ..firmed import FirmedGoal, FirmedParseError, derive_done_when, parse_firmed
from ..models import Goal, GoalStatus
from ..store import GoalStore
from ..transitions import Event
from . import PhaseResult

if TYPE_CHECKING:
    from ..tick import TickContext

ClaudeCaller = Callable[[str], Awaitable[str]]

#: when True, the discovery-resolve hook drops the goal into ``firming`` after
#: writing discovery.md (instead of straight into executing). Off by default so
#: legacy goals are unaffected — opt in per process or stack-wide.
FIRMING_ENABLED = os.environ.get("DEVCLAW_GOAL_FIRMING", "0") not in ("0", "false", "")

#: firming's model tier — defaults to opus (load-bearing cognition).
from ...model_tiers import model_for as _model_for
FIRMING_MODEL = _model_for("firming")


class FirmingError(Exception):
    """The firming model produced an unparseable / invalid draft. Carries the
    raw text on ``.raw`` for prompt-iteration."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def _load_for_transition(store: GoalStore, goal_id: str) -> "GoalStatus | None":
    """Reload the status RIGHT BEFORE a firming transition — no awaits may sit
    between this load and the ``store.transition()`` call that consumes it.

    Firming's transitions land after long cognition awaits (the firming round
    itself, then the decomposer), and the heartbeat bumps a parked firming
    goal's ``version`` on every tick (the ``can_run=False`` →
    ``update_status_fields(last_tick_at=...)`` path in
    ``tick._dispatch_phase_handler``). Transitioning against the pre-cognition
    snapshot therefore fails the version CAS on a PURE-TELEMETRY write —
    surfacing a spurious ``TransitionConflict`` to the ``answer_unknowns`` MCP
    caller for a race the owner didn't cause and can't see. Loading fresh here
    closes that window (single event loop: nothing can interleave between a
    sync load and a sync transition).

    Returns ``None`` when the goal reached a terminal phase mid-cognition —
    a concurrent ``cancel_goal`` must WIN, quietly: the caller records the
    firming result and leaves the goal untouched instead of resurrecting it
    (the stale-snapshot un-cancel class, firming edition)."""
    fresh = store.load_status(goal_id)
    if fresh.phase in ("done", "cancelled"):
        return None
    return fresh


def _build_prompt(
    goal: Goal,
    *,
    spec: str,
    discovery_brief: str,
    prior_draft: str,
    owner_answers: str,
    round_: int,
) -> str:
    from ...prompts import load_prompt

    return load_prompt(
        "firming",
        objective=goal.objective,
        done_when=goal.done_when or "(not specified)",
        verify_cmd=goal.verify_cmd or "(not specified)",
        round=round_,
        spec=spec or "(no spec — waiter scope-grill did not run)",
        discovery_brief=discovery_brief or "(no discovery brief yet)",
        prior_draft=prior_draft or "(none — round 1)",
        owner_answers=owner_answers or "(none — round 1)",
    )


def _render_prior_draft(draft: FirmedGoal | None) -> str:
    if draft is None:
        return ""
    from ..firmed import dump_firmed

    return dump_firmed(draft)


def _render_answers(answers: dict[str, str] | None) -> str:
    if not answers:
        return ""
    return "\n".join(f"- {k}: {v}" for k, v in sorted(answers.items()))


def _firmed_extras_block(draft: FirmedGoal) -> str:
    """Render the firmed-goal fields the decomposer needs beyond done_when —
    conventions to follow, capability blockers that require scaffolding items,
    and explicit descoped capabilities to NOT plan for. Empty string when the
    firmed draft populated none of these. Appended to the discovery brief so
    the decomposer interface stays agnostic of FirmedGoal."""
    parts: list[str] = []
    if draft.conventions_to_follow:
        parts.append("\n## Conventions to follow (from research — match existing patterns):")
        parts.extend(f"- {c}" for c in draft.conventions_to_follow)
    if draft.blockers:
        parts.append(
            "\n## Blockers (the repo can't currently do these — scaffold them as "
            "their own items before any tool that depends on them):"
        )
        parts.extend(f"- {b}" for b in draft.blockers)
    if draft.descoped:
        parts.append(
            "\n## Descoped (owner explicitly removed — do NOT plan items for these):"
        )
        parts.extend(f"- {d}" for d in draft.descoped)
    return "\n".join(parts)


async def _firm_once(
    goal: Goal,
    *,
    spec: str,
    discovery_brief: str,
    prior_draft: FirmedGoal | None,
    owner_answers: dict[str, str] | None,
    round_: int,
    caller: ClaudeCaller,
) -> FirmedGoal:
    """Run one firming cognition pass — round 1 (no prior draft, no answers) or
    round N (prior + answers). Raises :class:`FirmingError` on parse failure."""
    prompt = _build_prompt(
        goal,
        spec=spec,
        discovery_brief=discovery_brief,
        prior_draft=_render_prior_draft(prior_draft),
        owner_answers=_render_answers(owner_answers),
        round_=round_,
    )
    raw = await caller(prompt)
    try:
        return parse_firmed(raw)
    except FirmedParseError as exc:
        raise FirmingError(str(exc), raw) from exc


def default_caller() -> ClaudeCaller:
    """The production firming caller. Imported lazily from devclaw's shared
    ``claude --print`` factory so unit tests (which inject a fake) never touch
    the subprocess."""
    from ...planner import claude_with_model

    return claude_with_model(FIRMING_MODEL, role="goal_firming")


class FirmingHandler:
    """The firming phase, packaged as a PhaseHandler. ``can_run`` short-circuits
    when the goal is already parked on owner-answers (firming is event-driven,
    not polled); ``run`` does round 1; ``handle_answer`` does round N>=2 and is
    called by the MCP layer, not by tick."""

    name = "firming"

    def __init__(
        self,
        caller: ClaudeCaller | None = None,
        decomposer_caller: ClaudeCaller | None = None,
    ) -> None:
        self._caller = caller
        self._decomposer_caller = decomposer_caller

    def _resolve_caller(self) -> ClaudeCaller:
        if self._caller is None:
            self._caller = default_caller()
        return self._caller

    def _resolve_decomposer_caller(self) -> ClaudeCaller:
        """Lazy-resolve the decomposer caller — kept separate from the firming
        caller so the two can run on different model tiers (firming is Opus by
        default; decomposer defaults to its own DECOMPOSER_MODEL)."""
        if self._decomposer_caller is None:
            from .. import decomposer as _decomposer

            self._decomposer_caller = _decomposer.default_caller()
        return self._decomposer_caller

    async def can_run(
        self, goal: Goal, status: GoalStatus, store: GoalStore
    ) -> bool:
        """Fire firming round 1 ONCE per goal. The handler is past this gate
        only when the goal is in lifecycle=firming, NOT blocked (a blocked
        firming waits on answer_unknowns), and no firmed draft has been written
        yet. After the first run lands `phase=blocked` or `lifecycle=executing`,
        this returns False and the goal stays put until an event fires."""
        if status.lifecycle != "firming":
            return False
        if status.phase == "blocked":
            return False
        if status.in_flight is not None:
            return False
        return store.read_firmed_draft(goal.id) is None

    async def run(
        self, goal_id: str, goal: Goal, status: GoalStatus, ctx: "TickContext",
    ) -> PhaseResult:
        """Round 1 firming. Reads goal + spec + discovery, runs the firming
        prompt, persists the draft, and either advances to executing (clean
        firmed) or blocks with an owner ping (unknowns surfaced)."""
        store = ctx.store
        caller = self._caller or self._resolve_caller()

        try:
            draft = await _firm_once(
                goal,
                spec=store.read_spec(goal_id),
                discovery_brief=store.read_discovery(goal_id),
                prior_draft=None,
                owner_answers=None,
                round_=1,
                caller=caller,
            )
        except FirmingError as exc:
            store.append_log(goal_id, f"firming round 1 failed: {exc}")
            # Fall through to executing — firming is foundational but must not
            # wedge a goal forever (mirrors the discovery-synthesis degrade).
            # Transition from a JUST-loaded snapshot, not the pre-cognition
            # `status` (see _load_for_transition — a heartbeat telemetry bump
            # during the failed cognition await must not conflict this write).
            fresh = _load_for_transition(store, goal_id)
            if fresh is None:
                store.append_log(
                    goal_id,
                    "firming fallback skipped — goal reached a terminal phase mid-firming",
                )
                return PhaseResult(outcome="slept", note="goal terminal; firming abandoned")
            store.transition(
                goal_id, Event.FIRMING_ADVANCE,
                replace(fresh, lifecycle="executing", phase="idle"),
                expect=fresh,
            )
            await ctx.notifier.send(
                f"⚠️ [{goal_id}] firming failed ({exc}) — proceeding without it"
            )
            return PhaseResult(outcome="advanced", note="firming failed; falling back")

        store.write_firmed_draft(goal_id, draft)
        return await self._land(goal_id, draft, ctx, round_=1)

    async def handle_answer(
        self,
        goal_id: str,
        answers: dict[str, str],
        *,
        ctx: "TickContext",
    ) -> dict:
        """Round N>=2 firming, fired by the ``answer_unknowns`` MCP tool. The
        caller is responsible for verifying every current unknown id appears in
        ``answers`` (the tool surface enforces completeness); this method just
        merges + re-firms and returns the structured result the waiter renders.

        Returns a dict with ``status`` ('firmed' | 'needs_more_answers'),
        ``unknowns`` (populated when more answers are needed), and ``round``.

        The firming cognition awaits span minutes, and the heartbeat bumps a
        parked firming goal's version every tick — so ``_land`` transitions
        from a snapshot loaded RIGHT before the write (see
        :func:`_load_for_transition`), never from a pre-cognition one. A goal
        cancelled mid-cognition stays cancelled (the draft is still written —
        audit trail — but no transition fires); the returned dict then
        reflects the draft, not the goal's phase."""
        store = ctx.store
        caller = self._caller or self._resolve_caller()
        goal = store.load_goal(goal_id)
        prior = store.read_firmed_draft(goal_id)
        if prior is None:
            raise FirmingError(
                f"answer_unknowns called on goal {goal_id!r} without an existing "
                "firmed-draft.yaml — firming round 1 must run first"
            )

        next_round = prior.round + 1
        try:
            draft = await _firm_once(
                goal,
                spec=store.read_spec(goal_id),
                discovery_brief=store.read_discovery(goal_id),
                prior_draft=prior,
                owner_answers=answers,
                round_=next_round,
                caller=caller,
            )
        except FirmingError as exc:
            store.append_log(goal_id, f"firming round {next_round} failed: {exc}")
            raise

        store.write_firmed_draft(goal_id, draft)
        await self._land(goal_id, draft, ctx, round_=next_round)
        return {
            "goal_id": goal_id,
            "status": "firmed" if draft.status == "firmed" else "needs_more_answers",
            "round": draft.round,
            "unknowns": [
                {
                    "id": u.id, "question": u.question, "why": u.why,
                    "options": list(u.options),
                    "default_if_no_answer": u.default_if_no_answer,
                }
                for u in draft.unknowns
            ],
        }

    async def _fire_decomposer(
        self,
        goal_id: str,
        goal: Goal,
        draft: FirmedGoal,
        store: GoalStore,
    ) -> str:
        """Run the decomposer against the firmed goal. Carries the firmed
        outputs the decomposer can use: ``done_when`` synthesized from
        success_criteria, ``stub_acceptable`` for the stub-policy check, and a
        ``conventions / blockers / descoped`` postfix appended to the discovery
        brief so the decomposer plans inside the firmed constraints (follow
        existing patterns, scaffold for missing capabilities, skip what's
        out-of-scope). Graceful degrade: failure logs + returns a note, the
        goal still advances to executing (executor falls back to backlog mode,
        same as the legacy decompose-disabled path)."""
        from dataclasses import replace as _replace

        from .. import decomposer as _decomposer

        derived_done_when = derive_done_when(draft) or goal.done_when
        derived_stub_acceptable = (
            list(draft.stub_acceptable) if draft.stub_acceptable
            else list(goal.stub_acceptable)
        )
        derived_goal = _replace(
            goal, done_when=derived_done_when, stub_acceptable=derived_stub_acceptable,
        )
        brief = store.read_discovery(goal_id) or ""
        extras = _firmed_extras_block(draft)
        if extras:
            brief = (brief + "\n" + extras) if brief else extras
        try:
            caller = self._decomposer_caller or self._resolve_decomposer_caller()
            checklist = await _decomposer.decompose(
                derived_goal,
                claude_caller=caller,
                discovery_brief=brief,
            )
        except _decomposer.GoalDecomposerError as exc:
            store.append_log(
                goal_id,
                f"firming-side decomposer failed ({exc}) — proceeding to "
                "executing without checklist",
            )
            return "no checklist (decomposer failed)"
        store.write_checklist(goal_id, checklist)
        return f"decomposed → {len(checklist.items)} items"

    async def _land(
        self,
        goal_id: str,
        draft: FirmedGoal,
        ctx: "TickContext",
        *,
        round_: int,
    ) -> PhaseResult:
        """Common tail of run / handle_answer: write status + log + notify based
        on whether the new draft is firmed or still needs answers. When firmed,
        also fires the decomposer against the firmed goal BEFORE transitioning
        to executing — closes the gap where the firmed goal would otherwise
        reach executing with no checklist (silent regression to backlog mode).

        Both transitions run against a JUST-loaded snapshot (see
        :func:`_load_for_transition`) — the cognition awaits that precede this
        method (the firming round, then the decomposer below) are exactly the
        windows a heartbeat telemetry bump or a concurrent cancel lands in. A
        terminal goal short-circuits: the draft stays on disk (the audit
        trail), the status is left untouched."""
        store = ctx.store
        if draft.status == "firmed":
            goal = store.load_goal(goal_id)
            decompose_note = await self._fire_decomposer(goal_id, goal, draft, store)
            fresh = _load_for_transition(store, goal_id)
            if fresh is None:
                store.append_log(
                    goal_id,
                    f"firming round {round_} firmed, but the goal reached a "
                    "terminal phase mid-firming — leaving it untouched",
                )
                return PhaseResult(outcome="slept", note="goal terminal; firming result recorded only")
            store.transition(
                goal_id, Event.FIRMING_ADVANCE,
                replace(fresh, lifecycle="executing", phase="idle",
                        blocked_on=None, next="firming done → executing"),
                expect=fresh,
            )
            store.append_log(
                goal_id,
                f"firming round {round_} → firmed "
                f"({len(draft.success_criteria)} criteria, "
                f"{len(draft.conventions_to_follow)} conventions); "
                f"{decompose_note}",
            )
            await ctx.notifier.send(
                f"✅ [{goal_id}] firmed — {len(draft.success_criteria)} criteria, "
                f"starting decomposition"
            )
            return PhaseResult(outcome="advanced", note="firmed")

        # needs_owner_answers
        n = len(draft.unknowns)
        blocker_msg = (
            f"{n} question{'s' if n != 1 else ''} — reply in OpenClaw"
        )
        fresh = _load_for_transition(store, goal_id)
        if fresh is None:
            store.append_log(
                goal_id,
                f"firming round {round_} surfaced {n} unknown(s), but the goal "
                "reached a terminal phase mid-firming — leaving it untouched",
            )
            return PhaseResult(outcome="slept", note="goal terminal; firming result recorded only")
        store.transition(
            goal_id, Event.FIRMING_NEEDS_ANSWERS,
            replace(fresh, lifecycle="firming", phase="blocked",
                    blocked_on=blocker_msg, next=""),
            expect=fresh,
        )
        store.append_log(
            goal_id, f"firming round {round_} → needs_owner_answers ({n} unknown(s))",
        )
        await ctx.notifier.send(
            f"🟡 [{goal_id}] DevClaw needs you — {n} question{'s' if n != 1 else ''}. "
            f"Reply in OpenClaw."
        )
        return PhaseResult(outcome="blocked", note=blocker_msg)
