"""Action & investigation dispatch — the goal-tick engine-launch paths.

Everything that hands work to the engine (or resolves the discovery/decompose
step that precedes execution): the dispatch-cap backstop + atomic action
dispatch, the checklist in-flight flagging, the phase-handler shim, and the
investigating-phase paths (repo-research vs. from-scratch world-research, then
discovery synthesis + decomposition). Split out of :mod:`devclaw.goal.tick`;
imports tick_context + tick_guards, and is called by tick_settle /
tick._tick_goal_impl / tick._handle_executing via the tick.py re-export facade.
"""

from __future__ import annotations

from dataclasses import replace

from .tick_context import (
    NotifyLevel,
    Outcome,
    TickContext,
    WorkspacePrep,
    _action_label,
    _engine_kick,
    _notify,
    _run_atomic,
)
from .tick_guards import _block_on_prep_failure
from . import checklist as _checklist
from . import decomposer as _decomposer
from . import research as _research
from . import world_research as _world_research
from . import delivery_strategy as _delivery
from .engine import GoalEngine
from .models import Action, Goal, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalStore
from .transitions import Event
from ..engine.workspace import WorkspaceError
from ..loom import trace as _trace


def _flag_items_in_flight(store: GoalStore, goal_id: str, item_ids: list[str]) -> None:
    """When a dispatched action carries ``addresses``, mark those checklist
    items ``in_flight`` so the planner's ``ready_items`` filter excludes them
    on the next tick (no re-pick of the same item before settle). No-op when
    no checklist exists or the action has no addresses.

    PR7: this now ALWAYS runs INSIDE the dispatch transaction (its one
    production call site, in ``_dispatch_action``) — row-only writes
    (``mirror=False`` / ``write_checklist(..., render_view=False)``) so an
    aborted dispatch (CAS conflict, a raised ``engine.dispatch``) can't
    leave an item flagged ``in_flight`` for work that never actually
    dispatched (a small extra honesty win over the pre-PR7 behavior, where
    this write was NOT part of the dispatch's atomic unit and could survive
    a rollback). The caller's ``render_mirrors()``/``discard_pending_mirrors()``
    flushes or drops these deferred writes after the transaction resolves."""
    if not item_ids:
        return
    current = store.read_checklist(goal_id)
    if current is None:
        return
    updated = current
    for item_id in item_ids:
        try:
            updated = _checklist.update_item(updated, item_id, status="in_flight")
        except KeyError:
            # planner cited an unknown id — log + skip; the planner's next
            # round will pick a real one from ready_items
            store.append_log(
                goal_id,
                f"checklist warn: action addresses unknown item {item_id!r}",
                mirror=False,
            )
    store.write_checklist(goal_id, updated, render_view=False)


async def _dispatch_action(
    goal_id: str, goal: Goal, base: GoalStatus, action: Action,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep,
    summarize: "ClaudeCaller | None" = None,
    consume_steering: "list[int] | None" = None,
) -> Outcome:
    # Runaway backstop (mechanism, not cognition): never spawn more than the
    # goal's known-bounded work surface + a small margin without a human. A
    # looping planner can't burn unbounded quota — it blocks instead.
    #
    # In backlog mode the cap is ``len(backlog) + 2`` — tight, matched to the
    # owner's brain-dump of starting tasks. In checklist mode (Pillar 1) the
    # decomposer's checklist IS the bounded work surface and is typically much
    # larger than the backlog hint that produced it (29-item checklist from a
    # 5-item backlog is normal); take the MAX so a checklist goal doesn't
    # block every backlog-size dispatches. Live-found 2026-06-26 when
    # finance-sentry-mcp-v3 hit a cap=7 with 22 ready items left.
    #
    # The counter is progress-aware: a dispatch that settles successfully
    # (done, gate passed or gateless) is refunded on settle (see
    # _resolve_polling_action), so the cap measures OUTSTANDING failed or
    # gate-failed dispatches, not lifetime throughput. A healthy auto-merging
    # mission goal — including its own verification reviews — never trips it;
    # a planner looping on failures still does. Live-found 2026-07-07 (blocked
    # on merged work) and again 2026-07-09 (blocked on on_track reviews).
    base_cap = len(goal.backlog) + 2
    checklist = store.read_checklist(goal_id)
    cap = max(base_cap, len(checklist.items) + 2) if checklist else base_cap
    if base.actions_dispatched >= cap:
        store.append_log(goal_id, f"dispatch cap {cap} reached — blocking for review")
        store.transition(
            goal_id, Event.BLOCK,
            replace(base, phase="blocked", blocked_on=f"dispatch cap {cap} reached — review the open PRs"),
            expect=base, consume_steering=consume_steering,
        )
        await _notify(notifier, NotifyLevel.OWNER, f"🛑 [{goal_id}] dispatch cap ({cap}) reached — paused for your review", summarize=summarize)
        return Outcome.BLOCKED
    # Give the engine a pristine checkout. Legacy mode resets to origin/<default>
    # for per-action freshness; checklist mode (Pillar 1) checks out the goal's
    # ``goal/<id>`` branch instead so each item's commits STACK on the prior
    # items rather than fork off main and re-implement the foundation (the
    # 2026-06-26 finance-sentry-mcp-v3 PR-fan-out failure). Read-only
    # ``review_repository`` actions always run on the default branch — they
    # don't write — even when a checklist exists.
    branch_for_dispatch: str | None = None
    if action.tool != "review_repository":
        branch_for_dispatch = _delivery.resolve_strategy(store, goal_id).goal_branch(goal_id)
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url, branch_for_dispatch, goal.skills_required)
    except WorkspaceError as exc:
        return await _block_on_prep_failure(
            goal_id, base, exc, store=store, notifier=notifier, summarize=summarize,
        )
    # Atomic dispatch (PR7): task/program row creation + the DISPATCH
    # transition + the log row, as ONE transaction. A crash or CAS conflict
    # anywhere inside rolls the whole unit back — the single-task-orphan
    # class (task dispatched, ref write lost) becomes structurally
    # impossible on the in-process engine. `dispatch_exc` distinguishes
    # "engine.dispatch itself raised" (recovered below, outside the aborted
    # unit, exactly as today) from a TransitionConflict/IllegalTransition
    # raised by store.transition() (propagated UNTOUCHED to tick_goal's
    # top-level choke point, same as before this PR).
    dispatch_exc: "Exception | None" = None
    try:
        with store.transaction():
            try:
                ref = _run_atomic(engine.dispatch(action, goal, notify_url))
            except Exception as exc:  # noqa: BLE001 — caught again below, outside the txn
                dispatch_exc = exc
                raise
            # Carry the action's checklist addresses onto the in-flight ref so
            # the settle hook can update the right items without re-reading
            # the plan.
            if action.addresses:
                ref = replace(ref, addresses=list(action.addresses))
            store.transition(
                goal_id, Event.DISPATCH_ACTION,
                replace(
                    base, phase="in_flight", in_flight=ref, blocked_on=None, next=action.goal,
                    actions_dispatched=base.actions_dispatched + 1,
                ),
                expect=base, consume_steering=consume_steering,
            )
            # Checklist mode: flip addressed items to in_flight so the planner
            # doesn't re-pick them next tick before this one settles. No-op in
            # legacy mode. Row-only write — see _flag_items_in_flight.
            _flag_items_in_flight(store, goal_id, list(action.addresses))
            store.append_log(goal_id, f"dispatched {action.tool}: {action.goal} → {ref.id}", mirror=False)
    except Exception:
        store.discard_pending_mirrors(goal_id)
        if dispatch_exc is None:
            raise  # a real TransitionConflict/IllegalTransition — propagate untouched
        exc = dispatch_exc
        # engine.dispatch raised INSIDE the txn → it rolled back cleanly (no
        # row survives). Re-derive today's error path OUTSIDE the aborted
        # unit: a fresh, separate RESUME_IDLE write.
        store.append_log(goal_id, f"dispatch error ({action.tool}): {exc}")
        store.transition(
            goal_id, Event.RESUME_IDLE, replace(base, phase="idle", next=action.goal),
            expect=base, consume_steering=consume_steering,
        )
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] dispatch failed: {exc}")
        return Outcome.ERROR
    # Past this point the dispatch transaction has committed. Render the
    # deferred mirrors, THEN kick the queue to claim/launch the just-committed
    # row (pump=False left it merely 'pending'), then trace/notify — all
    # post-commit, per the mirror-discipline + dispatch/pump-split rules.
    store.render_mirrors(goal_id)
    _engine_kick(engine)
    _trace.record_dispatch(goal_id=goal_id, tool=action.tool, ref_id=ref.id, engine=getattr(engine, "kind", ""))
    # Notify uses the short label, not the full prompt body — the raw `action.goal`
    # is a multi-paragraph executor instruction (often 500-1500 chars) and dumping
    # it to Telegram floods the owner with prompt boilerplate. Full text stays in
    # log.md above for forensic readability.
    await _notify(
        notifier, NotifyLevel.TASK,
        f"🚀 [{goal_id}] {action.tool}: {_action_label(action)}",
    )
    return Outcome.DISPATCHED


async def _dispatch_phase_handler(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext, name: str,
) -> Outcome:
    """Hand a phase off to the registered :class:`PhaseHandler`. Tick is the
    dispatcher; the handler owns its own cognition, persistence, and notify.
    Maps the handler's string outcome back to an :class:`Outcome` enum so the
    rest of tick is unchanged.

    A missing handler is a silent no-op (sleep this tick) — that should never
    happen in practice (the Phase enum is the source of truth) but the conservative
    fallback keeps a typo from wedging a goal."""
    from .phases.registry import handler_for

    handler = handler_for(name)
    if handler is None:
        ctx.store.append_log(goal_id, f"no handler registered for phase {name!r} — sleeping")
        return Outcome.SLEPT
    if not await handler.can_run(goal, status, ctx.store):
        # The handler decided this isn't its tick (e.g. firming parked on
        # owner-answers). Mechanism, zero tokens.
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        return Outcome.IDLE
    result = await handler.run(goal_id, goal, status, ctx)
    try:
        return Outcome(result.outcome)
    except ValueError:
        ctx.store.append_log(
            goal_id,
            f"handler {name!r} returned unknown outcome {result.outcome!r} — treating as slept",
        )
        return Outcome.SLEPT


async def _open_world_research(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, notifier: Notifier,
    world_research_caller: "ClaudeCaller | None",
    summarize: "ClaudeCaller | None" = None,
) -> Outcome:
    """From-scratch investigation path — world-research instead of repo-research.

    Synchronous (no engine dispatch): one cognition call, write the brief to
    ``discovery.md``, transition the lifecycle the same way ``_resolve_discovery``
    does after a repo-research analysis completes.

    Failure is non-fatal — an empty brief means we proceed without one rather
    than wedging the goal. The owner sees the failure in the log.
    """
    caller = world_research_caller or _world_research.default_caller()
    spec = store.read_spec(goal_id)
    try:
        brief = await _world_research.world_brief(goal, spec, caller=caller)
        store.write_discovery(goal_id, brief)
        store.append_log(goal_id, "world-research brief written")
        synth_ok = True
    except Exception as exc:  # noqa: BLE001 — world-research must not wedge the goal
        store.append_log(goal_id, f"world-research failed ({exc}) — proceeding without brief")
        synth_ok = False

    # Same lifecycle transition logic as _resolve_discovery (repo-research's
    # resolution): firming sits between investigating and executing when
    # enabled. The decomposer runs against the FIRMED goal in that case, so
    # we don't fire it here.
    from .phases.firming import FIRMING_ENABLED as _FIRMING_ENABLED

    next_lifecycle = "firming" if _FIRMING_ENABLED else "executing"
    next_note = (
        "world-research done → firming" if _FIRMING_ENABLED
        else "world-research done → executing"
    )
    store.transition(
        goal_id, Event.RESOLVE_INVESTIGATION,
        replace(status, lifecycle=next_lifecycle, phase="idle", next=next_note),
        expect=status,
    )
    msg = (
        f"🌍 [{goal_id}] researched what good looks like for \"{goal.objective}\""
        f" — written exemplars + MVP bar + defer list. Starting work."
        if synth_ok
        else f"🌍 [{goal_id}] starting work on \"{goal.objective}\""
    )
    await _notify(notifier, NotifyLevel.OWNER, msg, summarize=summarize)
    return Outcome.ADVANCED


async def _open_investigation(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep, summarize: "ClaudeCaller | None" = None,
    world_research_caller: "ClaudeCaller | None" = None,
) -> Outcome:
    """A new outcome goal investigates before it executes.

    Two paths, branched on ``world_research.should_fire(goal)``:

    1. **From-scratch goal** (no ``repo_url``): fire WORLD-research
       synchronously. There's no repo to analyze, but the model's training
       knowledge of real software in this category gives the chain something
       concrete to align against (real exemplars, MVP bar, deliberately-defer
       list). Synthesis writes ``discovery.md`` and transitions the
       lifecycle the same way ``_resolve_discovery`` does after repo-research.

    2. **Existing-repo goal**: dispatch a read-only ``review_repository``
       analysis. Its terminal result feeds the discovery synthesis
       (``_resolve_discovery``) — repo-research, not world-research, because
       the ground truth is in the codebase. This is the senior-dev move:
       research, then act.

    A *dispatch* failure skips straight to executing — investigation is an
    enhancement, not a gate. A *prep* failure does NOT skip: the workspace it
    couldn't build is the same one executing needs, so deferring just hides
    the error one tick longer. Block on it instead (legibly).
    """
    # Branch 1: from-scratch goal → world-research, no engine dispatch.
    if _world_research.should_fire(goal):
        return await _open_world_research(
            goal_id, goal, status,
            store=store, notifier=notifier,
            world_research_caller=world_research_caller,
            summarize=summarize,
        )

    # Branch 2: existing-repo goal → today's path (prep workspace, dispatch
    # review_repository, await terminal, _resolve_discovery turns it into a
    # brief).
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url, None, goal.skills_required)
    except WorkspaceError as exc:
        return await _block_on_prep_failure(
            goal_id, status, exc, store=store, notifier=notifier, summarize=summarize,
        )
    review = Action(
        engine="devclaw", tool="review_repository",
        goal=(
            f"Read-only analysis: what does this repository actually do today?\n"
            f"The owner's desired outcome is: {goal.objective}\n"
            f"Describe the current functionality, structure, and how close it is to that outcome."
        ),
        open_pr=False,
    )
    # Atomic dispatch (PR7) — same shape as _dispatch_action's; see that
    # function's comment for the dispatch_exc/txn-nesting rationale.
    dispatch_exc: "Exception | None" = None
    try:
        with store.transaction():
            try:
                ref = _run_atomic(engine.dispatch(review, goal, notify_url))
            except Exception as exc:  # noqa: BLE001
                dispatch_exc = exc
                raise
            ref = replace(ref, is_discovery=True)
            store.transition(
                goal_id, Event.DISPATCH_DISCOVERY,
                replace(status, lifecycle="investigating", phase="in_flight", in_flight=ref), expect=status,
            )
            store.append_log(goal_id, f"investigating → repo analysis {ref.id}", mirror=False)
    except Exception:
        store.discard_pending_mirrors(goal_id)
        if dispatch_exc is None:
            raise
        exc = dispatch_exc
        store.append_log(goal_id, f"investigation dispatch failed ({exc}) — skipping to executing")
        store.transition(
            goal_id, Event.RESOLVE_INVESTIGATION,
            replace(status, lifecycle="executing", phase="idle"), expect=status,
        )
        return Outcome.SLEPT
    store.render_mirrors(goal_id)
    _engine_kick(engine)
    _trace.record_dispatch(goal_id=goal_id, tool=review.tool, ref_id=ref.id, engine=getattr(engine, "kind", ""), is_discovery=True)
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🔍 [{goal_id}] taking a look at what's there today — I'll come back with what it does and what 'better' could mean",
        summarize=summarize,
    )
    return Outcome.DISPATCHED


async def _resolve_discovery(
    goal_id: str, goal: Goal, status: GoalStatus, repo_analysis: str,
    *, store: GoalStore, research_caller: ClaudeCaller, notifier: Notifier,
    summarize: "ClaudeCaller | None" = None,
    decompose_enabled: bool = False,
    decomposer_caller: "ClaudeCaller | None" = None,
) -> Outcome:
    """The investigating analysis finished — synthesize the discovery brief
    (current state · gap-to-good · best-practice checklist) and persist it,
    optionally run the decomposer to emit a structured per-tool checklist
    (Pillar 1), then transition to executing. Synthesis and decomposition are
    each non-fatal — a failure in either falls back to backlog-driven mode."""
    brief = ""
    try:
        brief = await _research.discovery_brief(goal, repo_analysis, caller=research_caller)
        store.write_discovery(goal_id, brief)
        store.append_log(goal_id, "discovery brief written")
        synth_ok = True
    except Exception as exc:  # noqa: BLE001 — investigation must not wedge the goal
        store.append_log(goal_id, f"discovery synthesis failed ({exc}) — proceeding")
        synth_ok = False

    # Firming, when enabled, gates the decomposer: the decomposer should run
    # against the FIRMED goal, not the pre-firming goal.yaml. Skip the
    # in-discovery decomposer call in that case — a later session will wire the
    # decomposer to fire off firmed.yaml (proposal step 7).
    from .phases.firming import FIRMING_ENABLED as _FIRMING_ENABLED_FOR_DECOMPOSE

    decompose_ok = False
    if decompose_enabled and goal.done_when and not _FIRMING_ENABLED_FOR_DECOMPOSE:
        caller = decomposer_caller or research_caller
        try:
            cl = await _decomposer.decompose(
                goal,
                claude_caller=caller,
                discovery_brief=brief,
                repo_digest=repo_analysis,
            )
            store.write_checklist(goal_id, cl)
            store.append_log(
                goal_id,
                f"decomposer emitted checklist: {len(cl.items)} items, "
                f"{len(cl.open_questions)} open question(s)",
            )
            decompose_ok = True
        except Exception as exc:  # noqa: BLE001 — decomposition must not wedge the goal
            store.append_log(
                goal_id,
                f"decomposition failed ({exc}) — falling back to backlog mode",
            )

    # Firming sits between investigating and executing when enabled — its
    # handler will re-tick under lifecycle=firming on the very next loop turn
    # (the ADVANCED outcome we return below pokes the heartbeat).
    from .phases.firming import FIRMING_ENABLED as _FIRMING_ENABLED

    next_lifecycle = "firming" if _FIRMING_ENABLED else "executing"
    next_phase_note = (
        "discovery done → firming" if _FIRMING_ENABLED else "discovery done → executing"
    )
    store.transition(
        goal_id, Event.RESOLVE_INVESTIGATION,
        replace(status, lifecycle=next_lifecycle, phase="idle", next=next_phase_note),
        expect=status,
    )
    if decompose_ok:
        msg = (
            f"🔍 [{goal_id}] I looked at \"{goal.objective}\" — written what's there + "
            f"a structured plan ({len(store.read_checklist(goal_id).items)} items). Starting work."
        )
    elif synth_ok:
        msg = (
            f"🔍 [{goal_id}] I looked at what's there for \"{goal.objective}\" — "
            f"I've written up what it does today and what 'better' looks like. Starting work now."
        )
    else:
        msg = f"🔍 [{goal_id}] starting work on \"{goal.objective}\""
    await _notify(notifier, NotifyLevel.OWNER, msg, summarize=summarize)
    return Outcome.ADVANCED
