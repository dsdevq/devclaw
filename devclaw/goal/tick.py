"""The goal heartbeat — one wakeup.

Folded in from goalclaw, extended with grounded direction evaluation. Order is
load-bearing: the cheap, deterministic, ZERO-TOKEN check runs first and
short-circuits when there's nothing to do. Cognition (plan + evaluate) runs ONLY
past that gate. This is the quota guardrail — N idle ticks must cost ~0 tokens,
or the Pro weekly quota dies (burned this way 2026-05-18).

The evaluation tiers (mechanism gates cognition):
  1. progress check          — Python, every tick, 0 tokens (poll in-flight)
  2. per-delivery evidence    — in-proc, 0 tokens (write the grounded deliveries.md)
  3. direction eval (periodic)— 1 LLM call every EVAL_EVERY deliveries / on steering
  4. done-gate                — the planner's "done" is a proposal; it triggers a
                                read-only review whose report the evaluator judges;
                                only "achieved" actually closes the goal.

Everything is injected (store, engine, planner/evaluator callers, notifier,
prepare_ws) so a whole tick runs deterministically under test — no network, no
claude — and the quota assertion is just "FakeClaude.calls == 0" on idle paths.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from . import checklist as _checklist
from . import evaluator as _evaluator
from . import merge as _merge
from . import planner as _planner
from . import remote_checks as _remote_checks
from . import triage as _triage
# _deploy stays at tick.py level even though only tick_donegate._auto_deploy calls
# it: tests monkeypatch ``devclaw.goal.tick._deploy.deploy_project`` and both
# modules bind the SAME ..delivery.deploy module object, so patching it here is
# what makes the deploy stub visible to the moved _auto_deploy.
from ..delivery import deploy as _deploy  # noqa: F401 (re-export/monkeypatch anchor)
from .engine import GoalEngine
from .models import Action, Checklist as _ChecklistModel, Goal, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalDocCorrupt, GoalStore
from .transitions import Event, IllegalTransition, TransitionConflict
from ..loom import trace as _trace
from ..loom.limits import classify_failure, pause_seconds
from ..planner import PlannerError, planned_from_checklist as _planned_from_checklist
from ..state_store import _now_ms
from ..engine.workspace import prepare_workspace

# ---- extracted-module re-export facade (behavior-preserving split) --------
# Every symbol MOVED out of this file is re-exported here so
# ``devclaw.goal.tick.<name>`` (and ~20 test imports / monkeypatch targets)
# resolve exactly as before. Import graph stays acyclic:
# tick_context <- tick_guards <- {tick_dispatch, tick_donegate} <- tick_settle.
from .tick_context import (  # noqa: F401 (re-exported)
    AUTODEPLOY_ENABLED,
    DECOMPOSE_ENABLED,
    EVAL_EVERY,
    NO_PROGRESS_S,
    VERIFY_DONE,
    NotifyLevel,
    Outcome,
    Phase,
    TickContext,
    WorkspacePrep,
    _ALTITUDES,
    _action_label,
    _apply_corrections,
    _classify,
    _engine_kick,
    _notify,
    _notify_floor,
    _run_atomic,
    _TICK_LOCKS,
    _tick_lock,
    triaged_notify,
)
from .tick_guards import (  # noqa: F401 (re-exported)
    CORRUPT_DOC_HEAL_CAP,
    PREP_HEAL_CAP,
    _autoheal_corrupt_doc,
    _autoheal_prep,
    _block_on_corrupt_doc,
    _block_on_lost_ref,
    _block_on_prep_failure,
    _check_no_progress,
    _progress_window_active,
)
from .tick_donegate import (  # noqa: F401 (re-exported)
    _auto_deploy,
    _done_gate_review_brief,
    _open_done_gate,
    _project_owns_its_deploy,
    _resolve_done_gate,
)
from .tick_dispatch import (  # noqa: F401 (re-exported)
    _dispatch_action,
    _dispatch_phase_handler,
    _flag_items_in_flight,
    _open_investigation,
    _open_world_research,
    _resolve_discovery,
)
from .tick_settle import (  # noqa: F401 (re-exported)
    _readopt_orphaned_ref,
    _readopt_ref,
    _resolve_polling_action,
    _resolve_polling_discovery,
    _resolve_polling_done_gate,
    _settle_addressed_items,
    sweep_orphaned_refs,
)


async def tick_goal(
    goal_id: str,
    *,
    store: GoalStore,
    engine: GoalEngine,
    planner_caller: ClaudeCaller,
    evaluator_caller: ClaudeCaller,
    notifier: Notifier,
    notify_url: str = "",
    prepare_ws: WorkspacePrep = prepare_workspace,
    eval_every: int = EVAL_EVERY,
    verify_done: bool = VERIFY_DONE,
    autodeploy: bool = AUTODEPLOY_ENABLED,
    no_progress_s: int = NO_PROGRESS_S,
    decompose_enabled: bool = DECOMPOSE_ENABLED,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    decomposer_caller: "ClaudeCaller | None" = None,
    world_research_caller: "ClaudeCaller | None" = None,
    trend_detector: "object | None" = None,
    remote_checker: "_remote_checks.RemoteChecker | None" = None,
) -> Outcome:
    """Run one heartbeat and record a single ``tick`` trace event with the
    incoming (lifecycle, phase) and outgoing outcome — the only place the trace
    sees a tick. All the cognition / dispatch / delivery / notify events fired
    during the body land between this tick and the next.

    ``trend_detector`` (typed as ``object`` to avoid an import cycle with
    ``devclaw.trend_detector``): when set, runs per-project trend signals after
    the tick body settles. Telemetry-shaped: a detector exception NEVER breaks
    the tick — it is recorded as a note and swallowed.

    The ENTIRE body runs under this goal's :func:`_tick_lock` (PR8) — a
    concurrent tick for the SAME goal (tick_one racing tick_all's sweep) waits
    here instead of both running cognition and one losing its round to a
    TransitionConflict. See the lock's own comment for the full rationale.
    Different goals use different Lock objects, so this never serializes the
    fleet — only same-goal overlap."""
    async with _tick_lock(goal_id):
        status_before = store.load_status(goal_id)
        phase_before = _classify(status_before)
        lifecycle_before = status_before.lifecycle or "executing"
        try:
            outcome = await _tick_goal_impl(
                goal_id,
                store=store, engine=engine,
                planner_caller=planner_caller, evaluator_caller=evaluator_caller,
                notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
                eval_every=eval_every, verify_done=verify_done, autodeploy=autodeploy,
                no_progress_s=no_progress_s,
                decompose_enabled=decompose_enabled,
                summary_caller=summary_caller, merger=merger,
                decomposer_caller=decomposer_caller,
                world_research_caller=world_research_caller,
                remote_checker=remote_checker,
            )
        except IllegalTransition as exc:
            # A handler proposed an (event, target) the LEGAL table doesn't permit
            # from the goal's CURRENT stored state — always a bug (the handler
            # computed the wrong event, or LEGAL is missing a real code path),
            # never an expected race (see TransitionConflict below for that). Force
            # -block rather than let the tick loop crash-retry the same bug every
            # heartbeat — loud failure over silent degradation (CLAUDE.md's
            # hardening philosophy: verification fails closed, corruption blocks
            # legibly, and this is the state-machine's version of the same rule).
            store.append_log(goal_id, f"ILLEGAL transition — blocking: {exc}")
            store.force_block(goal_id, f"illegal state transition: {exc}")
            await _notify(
                notifier, NotifyLevel.OWNER,
                f"🟥 [{goal_id}] internal state error — I've paused this goal; steer to resume: {exc}",
                summarize=summary_caller,
            )
            outcome = Outcome.BLOCKED
        except TransitionConflict as exc:
            # Expected, not a bug: another writer (steer_goal / cancel_goal,
            # typically) committed between this tick's load and its write. The
            # tick's write is simply abandoned — nothing from this turn was
            # persisted — and the NEXT tick reads the fresh state instead of
            # clobbering it (the stale-snapshot un-cancel class this PR closes:
            # today, without this catch, the tick's stale write would silently
            # win and un-cancel the goal). Zero notify — benign and self-healing,
            # a notification here would just be tick-cadence noise. Note: the
            # PR8 lock makes a tick_one-vs-tick_all conflict on the SAME goal
            # unreachable (they now serialize); this catch remains load-bearing
            # for steer_goal/cancel_goal, which stay lock-free by design.
            store.append_log(goal_id, f"tick abandoned — state changed mid-tick: {exc}")
            outcome = Outcome.CONFLICT
        if trend_detector is not None:
            try:
                # Volume hygiene (2026-07-15): a terminal goal gets no trend
                # sweep — production showed ~350 trend_check rows per goal per
                # night across 17 goals of which 15 were cancelled/done. The
                # skip lives HERE (where the sweep selects goals), not inside
                # the detector; re-read the status so a goal that went terminal
                # DURING this very tick (done-gate closed it, cancel raced in)
                # is skipped too. Cheap SQLite read — zero LLM either way.
                if store.load_status(goal_id).phase not in ("done", "cancelled"):
                    goal = store.load_goal(goal_id)
                    await trend_detector.run_per_goal(
                        goal_id=goal_id, workspace_dir=goal.workspace_dir,
                    )
            except Exception as exc:  # noqa: BLE001 — telemetry must not break ticks
                _trace.record_note(
                    f"trend_detector.run_per_goal failed for {goal_id}: "
                    f"{exc.__class__.__name__}: {exc}"
                )
        _trace.record_tick(
            goal_id=goal_id, lifecycle=lifecycle_before,
            phase=phase_before.value, outcome=outcome.value,
        )
        return outcome


async def _tick_goal_impl(
    goal_id: str,
    *,
    store: GoalStore,
    engine: GoalEngine,
    planner_caller: ClaudeCaller,
    evaluator_caller: ClaudeCaller,
    notifier: Notifier,
    notify_url: str = "",
    prepare_ws: WorkspacePrep = prepare_workspace,
    eval_every: int = EVAL_EVERY,
    verify_done: bool = VERIFY_DONE,
    autodeploy: bool = AUTODEPLOY_ENABLED,
    no_progress_s: int = NO_PROGRESS_S,
    decompose_enabled: bool = DECOMPOSE_ENABLED,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    decomposer_caller: "ClaudeCaller | None" = None,
    world_research_caller: "ClaudeCaller | None" = None,
    remote_checker: "_remote_checks.RemoteChecker | None" = None,
) -> Outcome:
    """Run one heartbeat. Reads the goal's status, classifies it into a
    :class:`Phase`, dispatches to the matching handler.

    Two design pillars carried over from the original implementation:
      * **Terminal short-circuit** runs BEFORE the no-progress watchdog so done /
        cancelled goals don't even read the clock.
      * **Action-poll chains into EXECUTING** in the same tick — a settled
        regular action records its delivery, clears ``in_flight``, and the
        planner sees the just-finished detail without waiting another heartbeat.
        (Discovery / done-gate polls do NOT chain — they have dedicated
        resolution handlers.)
    """
    ctx = TickContext(
        store=store, engine=engine,
        planner_caller=planner_caller, evaluator_caller=evaluator_caller,
        notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
        eval_every=eval_every, verify_done=verify_done, autodeploy=autodeploy,
        no_progress_s=no_progress_s,
        decompose_enabled=decompose_enabled,
        summary_caller=summary_caller, merger=merger,
        decomposer_caller=decomposer_caller,
        world_research_caller=world_research_caller,
        remote_checker=remote_checker,
    )

    status = store.load_status(goal_id)
    phase = _classify(status)

    # Terminal short-circuit — skip even the watchdog (and the contract-file
    # probe below: a done/cancelled goal must keep skipping at zero cost even
    # if a leftover doc rots on disk).
    if phase is Phase.TERMINAL_DONE:
        return Outcome.SKIP_DONE
    if phase is Phase.TERMINAL_CANCELLED:
        return Outcome.SKIP_CANCELLED

    # Effective goal = goal.yaml overlaid with firmed.yaml outputs when firming
    # has landed. Every cognition + gating path inside this tick (planner,
    # evaluator, done-gate) reads from here, so firmed stub_acceptable / derived
    # done_when are honored — not silently shadowed by the original goal.yaml.
    #
    # Contract-file choke point (T0.4): a checklist.yaml / firmed-draft.yaml
    # that EXISTS but won't parse raises GoalDocCorrupt from the store, and
    # this is the ONE place the tick catches it. The read_checklist call is a
    # probe — its result is discarded; it exists so a torn checklist blocks
    # HERE, loudly, instead of reading as "no checklist" at the many
    # read_checklist sites further down (dispatch branch selection, cap
    # computation, settle, done-gate), which are all unreachable for a
    # corrupt-doc goal because of this guard.
    try:
        goal = store.load_effective_goal(goal_id)
        store.read_checklist(goal_id)
    except GoalDocCorrupt as exc:
        return await _block_on_corrupt_doc(
            goal_id, status, exc,
            store=store, notifier=notifier, summarize=summary_caller,
        )

    # Mechanical auto-heal (F8): lift a mechanical:* block whose condition no
    # longer holds — no LLM, ever (the mirror of the quota pause's
    # timestamp-compare auto-resume in tick_all), damped by the persisted
    # per-goal heal budget so a flapping condition can't turn the zero-token
    # blocked steady-state into a plan + ping per cycle. Two healable kinds:
    #   * corrupt_doc — the probe above IS the recheck: it just re-parsed the
    #     contract docs, at zero cost, before any cognition;
    #   * prep — the recheck costs a git subprocess (ls-remote), so it runs on
    #     the persisted next_heal_at exponential backoff, not every tick.
    # needs_answer / bug / lost_ref / dispatch_cap stay human-gated (see the
    # heal guards' docstrings). A refused heal (budget spent / window closed /
    # still broken) leaves the blocked status untouched and the tick idles
    # below at zero cognition, same as any blocked tick.
    if status.phase == "blocked":
        healed = None
        if status.blocked_kind == "mechanical:corrupt_doc":
            healed = await _autoheal_corrupt_doc(
                goal_id, status, store=store, notifier=notifier,
            )
        elif status.blocked_kind == "mechanical:prep":
            healed = await _autoheal_prep(
                goal_id, goal, status, store=store, notifier=notifier,
            )
        if healed is not None:
            status = healed
            phase = _classify(status)

    # Zero-token no-progress watchdog: pure timestamp math; fires one owner ping
    # if an executing goal hasn't shipped in too long. Mutates status; never
    # transitions phase.
    status = await _check_no_progress(
        goal_id, goal, status,
        store=store, notifier=notifier, window_s=no_progress_s, summarize=summary_caller,
    )

    # Polling phases — settle in-flight work first.
    if phase is Phase.POLLING_DISCOVERY:
        return await _resolve_polling_discovery(goal_id, goal, status, ctx)
    if phase is Phase.POLLING_DONE_GATE:
        return await _resolve_polling_done_gate(goal_id, goal, status, ctx)

    # Orphaned-ref reconcile used to run HERE, every tick (2026-07-09:
    # closeloop-mission-v2 waited all night on a program that had already
    # failed, because STATUS.md was truncated by a crash mid-write). PR7's
    # atomic dispatch (task/program row + the DISPATCH transition + the log
    # row as ONE unit) makes that class of loss structurally impossible on
    # the in-process engine going forward — the in_flight ref can no longer
    # commit without the row it points at (and vice versa). The remaining
    # recovery surface — a goal whose ref was lost by an OLDER, pre-PR7
    # build, or by something outside the dispatch path entirely (manual DB
    # surgery, a cross-environment restore) — is handled ONCE per service
    # start by sweep_orphaned_refs, not every tick — see GoalService._loop().

    finished_detail = ""
    if phase is Phase.POLLING_ACTION:
        outcome = await _resolve_polling_action(goal_id, goal, status, ctx)
        if isinstance(outcome, Outcome):
            return outcome
        # A regular action settled; chain to the lifecycle phase that the just-
        # cleared status now classifies into (usually EXECUTING).
        status, finished_detail = outcome
        phase = _classify(status)

    # Lifecycle phases (in_flight is None).
    if phase is Phase.INVESTIGATING:
        return await _open_investigation(
            goal_id, goal, status,
            store=store, engine=engine, notifier=notifier,
            notify_url=notify_url, prepare_ws=prepare_ws, summarize=summary_caller,
            world_research_caller=world_research_caller,
        )
    if phase is Phase.FIRMING:
        return await _dispatch_phase_handler(goal_id, goal, status, ctx, "firming")
    if phase is Phase.EXECUTING:
        return await _handle_executing(goal_id, goal, status, finished_detail, ctx)

    raise RuntimeError(f"unhandled phase {phase} for goal {goal_id}")


# ---- evaluation helpers ----------------------------------------------------


async def _run_mid_flight_eval(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, evaluator_caller: ClaudeCaller, notifier: Notifier,
    summarize: "ClaudeCaller | None" = None,
) -> "Outcome | None":
    """Periodic, artifact-grounded direction check. Returns an Outcome to return
    early (blocked) or None to keep going. Resets the delivery counter."""
    # Ground the direction check in the goal's ACTUAL workspace (triage F3):
    # a mid-flight "correction" inferred from the wrong repo is written into
    # steering and burns real tasks, and a wrong-repo stalled/needs_human
    # falsely blocks the goal. Best-effort — never raises. Zero-token guard
    # holds: this runs only past the eval cadence gate, where cognition
    # already fires; the git subprocess adds no LLM call and no idle cost.
    repo_context = await _evaluator._repo_context(goal.workspace_dir)
    try:
        ev = await _evaluator.evaluate(
            goal, status, store.recent_log(goal_id), store.recent_deliveries(goal_id),
            claude_caller=evaluator_caller, spec=store.read_spec(goal_id),
            repo_context=repo_context,
        )
    except _evaluator.GoalEvalError as exc:
        store.append_log(goal_id, f"eval error: {exc}")
        return None  # a bad eval must not stall the goal — continue to planning
    now = store.now_iso()
    base = replace(
        status, last_eval_verdict=ev.verdict, last_eval_at=now,
        last_eval_note=ev.rationale[:300], deliveries_since_eval=0,
    )
    store.append_log(goal_id, f"direction: {ev.verdict} — {ev.rationale[:200]}")
    if ev.verdict in ("stalled", "needs_human"):
        q = ev.question or ev.rationale or "direction evaluation flagged a problem"
        store.transition(
            goal_id, Event.BLOCK,
            replace(base, phase="blocked", blocked_on=q, blocked_kind="needs_answer", next=""),
            expect=status,
        )
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] direction check ({ev.verdict}) — {q}", summarize=summarize)
        return Outcome.BLOCKED
    # Telemetry-only (verdict/note/counter reset) — no phase/lifecycle/in_flight
    # change, so this goes through the column-only path, not a transition.
    store.update_status_fields(
        goal_id, last_eval_verdict=ev.verdict, last_eval_at=now,
        last_eval_note=ev.rationale[:300], deliveries_since_eval=0,
    )
    _apply_corrections(store, goal_id, ev)
    if ev.verdict == "off_track" and ev.corrections:
        await _notify(notifier, NotifyLevel.TASK, f"🧭 [{goal_id}] course-correcting — {ev.rationale[:200]}")
    return None


# ---- registry-driven phase dispatch ----------------------------------------


# ---- phase handlers --------------------------------------------------------
# One handler per Phase value. Each takes (goal_id, goal, status, ctx) — except
# the polling handlers, which the orchestrator calls with status already loaded
# — and returns either an :class:`Outcome` (terminal for this tick) or, for
# ``_resolve_polling_action``, an ``(updated_status, finished_detail)`` tuple
# so the EXECUTING handler can chain on the same tick.


async def _handle_one_shot_executing(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> Outcome:
    """The one-shot executing path (ADR 0003 stage 2) — ZERO per-tick planner
    cognition, ever. The checklist the decomposer emitted IS the plan:

      pending items → dispatch them ALL as ONE planned program (the queue runs
      the DAG in parallel; per-item verdicts come back via plan_key);
      checklist drained → propose done MECHANICALLY (the proposal is free; the
      close is still gated on the grounded done-gate review + evaluator);
      blocked items / blocked goal → idle at zero cost (the per-item circuit
      breaker already parked the goal with an owner ping).

    A failed child returns its items to the pick-pool with the failure logged,
    so the NEXT tick re-dispatches just the remainder as a smaller program —
    bounded by the per-item circuit breaker (ITEM_MAX_ATTEMPTS) and the
    dispatch cap, same brakes as the per-tick loop. Steering is deliberately
    NOT consumed here: there is no planner to apply it to — it stays unread in
    the inbox for the owner-visible record (stage 3 gives it a checkpoint)."""
    store = ctx.store
    if status.phase == "blocked":
        # Only resume_goal/steer_goal (or a mechanical heal upstream) unblocks
        # a one-shot goal — never the timer. Zero cost, same as the long-lived
        # blocked steady-state.
        store.update_status_fields(goal_id, last_tick_at=store.now_iso())
        return Outcome.IDLE
    checklist = store.read_checklist(goal_id)
    if checklist is None or not checklist.items:
        if not (store.read_discovery(goal_id) or "").strip():
            # No checklist AND no discovery brief: investigation never
            # produced its output — a prep failure blocked the discovery
            # dispatch and the mechanical heal resumed PAST it (the prep
            # block pins lifecycle=executing for blocked-routing; live-found
            # 2026-07-19 shakedown). Go back and investigate — decompose
            # rides that path (one_shot implies it). Bounded: a still-broken
            # workspace re-enters the prep-block/heal budget, and a completed
            # investigation whose DECOMPOSE fails leaves a brief behind, so
            # the next pass falls through to the loud block below (no loop).
            store.append_log(
                goal_id,
                "one-shot: no checklist and no discovery brief — re-entering "
                "investigation to rebuild the plan",
            )
            store.transition(
                goal_id, Event.REOPEN_INVESTIGATION,
                replace(status, lifecycle="investigating", phase="idle",
                        next="re-run investigation (one-shot plan missing)"),
                expect=status,
            )
            return Outcome.ADVANCED
        # A one-shot goal with no plan can never progress — the per-tick loop's
        # backlog fallback doesn't exist here. Fail loud, not idle-forever.
        reason = (
            "one-shot goal has no checklist — decomposition failed or never "
            "ran; cancel and re-file (or steer with a concrete plan)"
        )
        store.append_log(goal_id, f"one-shot: {reason}")
        store.transition(
            goal_id, Event.BLOCK,
            replace(status, phase="blocked", blocked_on=reason, blocked_kind="bug", next=""),
            expect=status,
        )
        await _notify(
            ctx.notifier, NotifyLevel.OWNER, f"🟥 [{goal_id}] {reason}",
            summarize=ctx.summary_caller,
        )
        return Outcome.BLOCKED
    # Crash artifact: items flagged in_flight while the goal holds no ref (the
    # ref settled or was lost; EXECUTING classification proves in_flight is
    # None). Return them to the pick-pool — mechanical, zero LLM.
    stale = [i.id for i in checklist.items if i.status == "in_flight"]
    if stale:
        for iid in stale:
            checklist = _checklist.update_item(checklist, iid, status="not_started")
        store.write_checklist(goal_id, checklist)
        store.append_log(
            goal_id,
            f"one-shot: reset stale in-flight item(s) {', '.join(stale)} — no live dispatch holds them",
        )
    pending = [i for i in checklist.items if i.status == "not_started"]
    # NEVER dispatch work whose prerequisite is known-failed (same contract as
    # checklist.ready_items): exclude items depending — transitively — on a
    # breaker-BLOCKED item. Reachable through the normal recovery flow: the
    # breaker parks the goal, resume_goal re-attempts WITHOUT resetting the
    # tripped item, and without this the remainder program would burn attempts
    # on dependents of a prerequisite that never shipped.
    blocked_ids = {i.id for i in checklist.items if i.status == "blocked"}
    if blocked_ids:
        excluded = set(blocked_ids)
        changed = True
        while changed:
            changed = False
            for i in pending:
                if i.id not in excluded and any(d in excluded for d in i.depends_on):
                    excluded.add(i.id)
                    changed = True
        skipped = [i.id for i in pending if i.id in excluded]
        if skipped:
            store.append_log(
                goal_id,
                "one-shot: holding item(s) "
                f"{', '.join(skipped)} — their dependency chain includes a "
                f"circuit-breaker-blocked item ({', '.join(sorted(blocked_ids))})",
            )
        pending = [i for i in pending if i.id not in excluded]
    if pending:
        ids = {i.id for i in pending}
        # Deps on already-done items are satisfied; drop them so order_tasks
        # doesn't see dangling refs (blocked-dep items were excluded above).
        filtered = [
            replace(i, depends_on=[d for d in i.depends_on if d in ids])
            for i in pending
        ]
        try:
            planned = _planned_from_checklist(_ChecklistModel(items=filtered))
        except PlannerError as exc:
            # The MAX_PROGRAM_TASKS brake (or a cycle) — mechanical, so it
            # would reproduce identically every heartbeat: an unhandled raise
            # here error-loops the tick forever with one log line and no
            # owner ping. Block loudly instead; the owner re-scopes.
            reason = f"one-shot plan rejected: {exc}"
            store.append_log(goal_id, reason)
            store.transition(
                goal_id, Event.BLOCK,
                replace(status, phase="blocked", blocked_on=reason,
                        blocked_kind="needs_answer", next=""),
                expect=status,
            )
            await _notify(
                ctx.notifier, NotifyLevel.OWNER, f"🛑 [{goal_id}] {reason}",
                summarize=ctx.summary_caller,
            )
            return Outcome.BLOCKED
        action = Action(
            engine="devclaw",
            tool="start_program",
            goal=(
                f"one-shot batch: {len(pending)} checklist item(s) toward: "
                f"{goal.objective[:200]}"
            ),
            verify_cmd=goal.verify_cmd,
            open_pr=goal.open_pr,
            addresses=[i.id for i in pending],
            planned=planned,
        )
        return await _dispatch_action(
            goal_id, goal, status, action,
            store=store, engine=ctx.engine, notifier=ctx.notifier,
            notify_url=ctx.notify_url, prepare_ws=ctx.prepare_ws,
            summarize=ctx.summary_caller,
        )
    if any(i.status == "blocked" for i in checklist.items):
        # The breaker's own settle path already parked the goal + pinged the
        # owner; reaching here means a racing unblock — idle, don't re-dispatch.
        store.update_status_fields(goal_id, last_tick_at=store.now_iso())
        return Outcome.IDLE
    # Checklist drained → mechanical done proposal. "Done" stays a PROPOSAL:
    # the grounded done-gate review + evaluator decide, same as when the
    # per-tick planner proposes it.
    store.append_log(goal_id, "one-shot: checklist drained — proposing done")
    return await _open_done_gate(
        goal_id, goal, status,
        store=store, engine=ctx.engine, evaluator_caller=ctx.evaluator_caller,
        notifier=ctx.notifier, notify_url=ctx.notify_url,
        prepare_ws=ctx.prepare_ws, verify_done=ctx.verify_done,
        note="one-shot: checklist drained",
        summarize=ctx.summary_caller, remote_checker=ctx.remote_checker,
        autodeploy=ctx.autodeploy,
    )


async def _handle_executing(
    goal_id: str, goal: Goal, status: GoalStatus, finished_detail: str, ctx: TickContext,
) -> Outcome:
    """The cognition path. Gate by work-present + cadence (preserves the
    zero-token guard — blocked goals only unblock on real work, never on the
    timer), then optionally run a periodic direction eval, then plan one
    action and dispatch on the planner's decision.

    PR5: steering is read as exact ``goal_steering`` row ids (``rows``), not
    a count. ``consume_ids`` — the ids as of the read that actually informed
    the plan — rides the post-plan transition (``consume_steering=``) so
    consumption lands atomically with the decision: a row inserted AFTER the
    read (e.g. during the planner's cognition await) keeps ``consumed_at``
    NULL and is seen next tick, whether or not this tick's own write
    survives its CAS. On the plan-error path below, no transition fires at
    all, so ``rows`` simply stays unconsumed — same net effect."""
    if goal.mode == "one_shot":
        # ADR 0003 stage 2: the one-shot dial replaces the per-tick planner
        # entirely — mechanical dispatch/done-proposal, zero LLM on this path.
        return await _handle_one_shot_executing(goal_id, goal, status, ctx)
    rows = ctx.store.unread_steering_rows(goal_id)
    steering = "\n".join(line for _, line in rows)
    # unread_steering_rows() may have lazily ingested new inbox.md lines,
    # which bumps goal_status.version (every write bumps version — PR4's
    # rule). Reload so `expect=` below CAS's against the CURRENT row, not a
    # pre-ingest snapshot — otherwise this tick's OWN ingest would look like
    # a concurrent writer and self-inflict a TransitionConflict on every
    # tick that finds fresh steering, not just a genuine race.
    status = ctx.store.load_status(goal_id)
    work = bool(finished_detail) or bool(steering)
    if status.phase == "blocked":
        should_plan = work  # cadence does NOT re-poke a blocked goal; only work unblocks
    else:
        should_plan = work or ctx.store.cadence_due(goal, status)
    if not should_plan:
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        return Outcome.IDLE

    # Periodic, artifact-grounded direction eval (mid-flight). Past the gate,
    # and only when enough has shipped, judge direction from the grounded
    # deliveries. Corrections become steering; a hard verdict blocks.
    if ctx.eval_every > 0 and status.deliveries_since_eval >= ctx.eval_every:
        blocked = await _run_mid_flight_eval(
            goal_id, goal, status,
            store=ctx.store, evaluator_caller=ctx.evaluator_caller, notifier=ctx.notifier,
            summarize=ctx.summary_caller,
        )
        status = ctx.store.load_status(goal_id)  # eval may have written status + steering
        if blocked is not None:
            return blocked
        rows = ctx.store.unread_steering_rows(goal_id)  # re-read
        steering = "\n".join(line for _, line in rows)
        status = ctx.store.load_status(goal_id)  # ingest (if any) may have bumped version again

    consume_ids = [rid for rid, _ in rows]

    # Plan one next action. Pass the checklist if one exists — the planner
    # then runs in checklist mode and picks one ready item. Also surface the
    # per-project trend retrospective (trend-PR3 — closes the cross-session
    # loop: the detector writes into <workspace>/.devclaw/trends.md; here we
    # feed the tail back into the planner so it can act on its own findings).
    # Pass "" when the file is missing OR holds only the placeholder so the
    # prompt skips the section entirely.
    from ..trend_detector import read_trends_text
    trends_text = read_trends_text(goal.workspace_dir, limit_chars=2000)
    if trends_text.startswith("(no trends recorded") or trends_text.startswith("(could not read"):
        trends_text = ""
    # Live workspace snapshot (triage F5): grounded plan-time facts from the
    # ACTUAL repo (remote, head, key-file probes, layout), so the planner's
    # instruction can't inherit repo assumptions from host-side claude's own
    # environment — on the fallback paths (investigation dispatch failed,
    # discovery synthesis failed, investigate disabled, from-scratch) the
    # prompt otherwise has no workspace-derived facts at all. Collected HERE,
    # past the should_plan gate beside the trends read, so idle/blocked ticks
    # stay zero-cost: no git subprocess, no LLM. Best-effort — never raises;
    # "" just omits the prompt section.
    repo_context = await _planner._collect_repo_context(goal.workspace_dir)
    try:
        result = await _planner.plan(
            goal, status, ctx.store.recent_log(goal_id), steering, finished_detail,
            claude_caller=ctx.planner_caller, discovery=ctx.store.read_discovery(goal_id),
            checklist=ctx.store.read_checklist(goal_id),
            trends=trends_text,
            repo_context=repo_context,
        )
    except (_planner.GoalPlannerError, PlannerError) as exc:
        # A usage/rate limit at the PLANNER must pause the layer, not be logged
        # and retried next tick (re-burning quota). The goal planner shells out
        # via the shared claude --print caller, which raises PlannerError on a
        # non-zero exit (e.g. a session limit). Catch BOTH (dogfood 2026-06-21).
        paused = _maybe_pause(ctx.engine, ctx.store, goal_id, str(exc))
        if paused is not None:
            ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
            return paused
        ctx.store.append_log(goal_id, f"plan error: {exc}")
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        await _notify(ctx.notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] plan step failed: {exc}")
        return Outcome.ERROR

    now = ctx.store.now_iso()
    # No `inbox_cursor=` here (PR5): that field now carries the INGEST cursor
    # (bumped by _ingest_inbox/append_steering, reflected via the `status`
    # reload above), not a consume cursor — consumption is `consume_ids`
    # riding each transition below, atomic with the decision.
    base = replace(status, last_plan_at=now, last_tick_at=now)

    if result.decision == "sleep":
        ctx.store.transition(
            goal_id, Event.RESUME_IDLE, replace(base, phase="idle", next=result.note),
            expect=status, consume_steering=consume_ids,
        )
        ctx.store.append_log(goal_id, f"sleep: {result.note}")
        return Outcome.SLEPT

    if result.decision == "blocked":
        ctx.store.transition(
            goal_id, Event.BLOCK,
            replace(base, phase="blocked", blocked_on=result.question,
                    blocked_kind="needs_answer", next=""),
            expect=status, consume_steering=consume_ids,
        )
        ctx.store.append_log(goal_id, f"blocked: {result.question}")
        await _notify(
            ctx.notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] needs you — {result.question}",
            summarize=ctx.summary_caller,
        )
        return Outcome.BLOCKED

    if result.decision == "done":
        return await _open_done_gate(
            goal_id, goal, base,
            store=ctx.store, engine=ctx.engine, evaluator_caller=ctx.evaluator_caller,
            notifier=ctx.notifier, notify_url=ctx.notify_url, prepare_ws=ctx.prepare_ws,
            verify_done=ctx.verify_done, note=result.note, summarize=ctx.summary_caller,
            remote_checker=ctx.remote_checker,
            autodeploy=ctx.autodeploy,
            consume_steering=consume_ids,
        )

    # decision == "act"
    return await _dispatch_action(
        goal_id, goal, base, result.actions[0],
        store=ctx.store, engine=ctx.engine, notifier=ctx.notifier,
        notify_url=ctx.notify_url, prepare_ws=ctx.prepare_ws, summarize=ctx.summary_caller,
        consume_steering=consume_ids,
    )


# ---- multi-goal driver -----------------------------------------------------


async def tick_all(
    *,
    store: GoalStore,
    engine: GoalEngine,
    planner_caller: ClaudeCaller,
    evaluator_caller: ClaudeCaller,
    notifier: Notifier,
    notify_url: str = "",
    prepare_ws: WorkspacePrep = prepare_workspace,
    eval_every: int = EVAL_EVERY,
    verify_done: bool = VERIFY_DONE,
    autodeploy: bool = AUTODEPLOY_ENABLED,
    no_progress_s: int = NO_PROGRESS_S,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    merger_resolver: "Callable[[Goal], _merge.Merger | None] | None" = None,
    verify_done_resolver: "Callable[[Goal], bool] | None" = None,
    autodeploy_resolver: "Callable[[Goal], bool] | None" = None,
    tracer_factory: "Callable[[str], _trace.Tracer | None] | None" = None,
    trend_detector: "object | None" = None,
    remote_checker: "_remote_checks.RemoteChecker | None" = None,
    triage_caller: "ClaudeCaller | None" = None,
) -> dict[str, Outcome]:
    """Tick every goal. One goal's failure never stops the others, and a usage
    limit pauses the whole layer (0 tokens) rather than crashing per-goal.

    ``tracer_factory(goal_id) -> Tracer | None`` is the seam GoalService uses
    to attach a :class:`PersistentTracer` per goal-tick so the cascade's
    cognition / dispatch / delivery events land in the durable trace store.

    ``merger_resolver``, when given, computes automerge FRESH per goal (a
    project's automerge override must not leak from one goal onto another in
    the same sweep) and takes precedence over the flat ``merger``. Plain
    ``merger`` stays supported for callers (and existing tests) with a single
    fleet-wide value. ``verify_done_resolver`` and ``autodeploy_resolver`` are
    the same idea for the done-gate re-check flag and the on-complete deploy
    flag: fresh per goal, each taking precedence over its flat counterpart.

    ``trend_detector`` (typed as ``object`` to avoid the import cycle with
    ``devclaw.trend_detector``): when set, runs per-project signals inside each
    per-goal tracer scope, and runs harness-self signals once after the loop
    inside a sentinel-keyed (``_harness_self_``) tracer scope. Telemetry-shaped
    catches: a detector exception NEVER breaks the heartbeat.
    """
    outcomes: dict[str, Outcome] = {}

    # Unified quota pause: the OAuth quota is account-wide, so if anything (a task
    # or earlier goal cognition) paused dispatch, skip ALL goal cognition until it
    # lifts — zero tokens while paused. Auto-clear + resume once it expires.
    until, reason = _engine_pause(engine)
    if until and _now_ms() < until:
        # Tell the owner ONCE per pause (a weekly cap can halt everything for
        # days — silence here looks like devclaw died). The goal layer owns the
        # Notifier, so the ping lives here and covers pauses set by EITHER
        # layer (task queue or goal cognition). The persisted flag is what
        # keeps this to one ping, not one per tick.
        if not _engine_pause_notified(engine):
            resume_hhmm = datetime.fromtimestamp(
                until / 1000, tz=timezone.utc
            ).strftime("%H:%M")
            await _notify(
                notifier, NotifyLevel.OWNER,
                f"⏸️ paused on a usage limit — {reason}; resuming ~{resume_hhmm} UTC",
                summarize=summary_caller,
            )
            _engine_set_pause_notified(engine, True)
        return {gid: Outcome.RATE_LIMITED for gid in store.list_goal_ids()}
    if until:
        _engine_clear_pause(engine)
    # Resume ping — the counterpart of the pause ping above, once per pause.
    # Checked whenever no pause is ACTIVE (not only on the expiry tick that
    # cleared it): the task queue lazily clears an expired pause too, and the
    # owner must still hear the resume in that race.
    if _engine_pause_notified(engine):
        await _notify(
            notifier, NotifyLevel.OWNER,
            "▶️ usage limit lifted — resuming work",
            summarize=summary_caller,
        )
        _engine_set_pause_notified(engine, False)

    # Operator controls: a manual pause toggle or a daily run-window can hold ALL
    # goal cognition (0 tokens) the same way the quota pause does. Tasks already
    # dispatched finish; nothing new is planned while gated. Re-checked every tick.
    blocked, _why = _engine_operator_block(engine)
    if blocked:
        return {gid: Outcome.RATE_LIMITED for gid in store.list_goal_ids()}

    # Retention (volume hygiene): AFTER the cheap gates above, BEFORE any
    # per-goal work — daily, batched, pure-SQLite DELETEs of the two
    # highest-volume append-only logs past their retention windows: traces
    # (DEVCLAW_TRACE_RETENTION_DAYS, 2026-07-15) and events (raw runner SDK
    # events, DEVCLAW_EVENTS_RETENTION_DAYS, 2026-07-18). Zero LLM calls, so the
    # zero-token idle guarantee is untouched; StateStore owns the actual writes
    # (single-writer invariant), the engine is just the seam.
    _engine_prune_traces(engine)
    _engine_prune_events(engine)
    # Reclaim the disk those DELETEs free — a weekly, freelist-gated VACUUM
    # (SQLite reuses freed pages but never shrinks the .db file on its own).
    # Same cheap-path slot, same zero-LLM guarantee.
    _engine_vacuum(engine)
    # Loud-not-silent DB-size alarm: if the .db has grown past the threshold
    # despite retention+VACUUM, ping the owner ONCE (re-armed when it drops back
    # under) — a silent disk-fill wedge is the failure mode this whole tranche
    # exists to prevent. Zero LLM (raw owner ping, no summarizer).
    await _maybe_alert_db_size(engine, notifier, triage_caller=triage_caller)

    for goal_id in store.list_goal_ids():
        # Per-goal run-window: a goal can carry its OWN night/off-hours schedule
        # on top of the engine-wide gate above (e.g. a token-heavy standing loop
        # confined to nights while other goals run all day). Outside its window,
        # skip just this goal — 0 tokens for it — while the others still tick.
        g_blocked, _gwhy = _engine_goal_operator_block(engine, goal_id)
        if g_blocked:
            outcomes[goal_id] = Outcome.RATE_LIMITED
            continue
        tracer = tracer_factory(goal_id) if tracer_factory else None
        goal_merger = merger
        goal_verify_done = verify_done
        goal_autodeploy = autodeploy
        # Load the goal once for whichever per-goal resolvers are wired (a bad
        # goal.yaml must not sink the sweep — fall back to the flat values).
        if any(r is not None for r in (merger_resolver, verify_done_resolver, autodeploy_resolver)):
            try:
                _g = store.load_goal(goal_id)
                if merger_resolver is not None:
                    goal_merger = merger_resolver(_g)
                if verify_done_resolver is not None:
                    goal_verify_done = verify_done_resolver(_g)
                if autodeploy_resolver is not None:
                    goal_autodeploy = autodeploy_resolver(_g)
            except Exception:  # noqa: BLE001 — a bad goal.yaml must not sink the sweep
                goal_merger, goal_verify_done, goal_autodeploy = merger, verify_done, autodeploy
        try:
            with _trace.tracer_scope(tracer):
                outcomes[goal_id] = await tick_goal(
                    goal_id, store=store, engine=engine,
                    planner_caller=planner_caller, evaluator_caller=evaluator_caller,
                    notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
                    eval_every=eval_every, verify_done=goal_verify_done,
                    autodeploy=goal_autodeploy, no_progress_s=no_progress_s,
                    summary_caller=summary_caller, merger=goal_merger,
                    trend_detector=trend_detector,
                    remote_checker=remote_checker,
                )
        except Exception as exc:  # noqa: BLE001 — isolate per-goal blast radius
            # the goal's OWN cognition (claude --print) hitting a limit pauses the
            # whole layer instead of crash-looping + burning quota; anything else is
            # logged with its real cause (never a blind 'crashed') and isolated.
            paused = _maybe_pause(engine, store, goal_id, str(exc))
            if paused is not None:
                outcomes[goal_id] = paused
            else:
                store.append_log(goal_id, f"tick error (isolated): {str(exc)[:160]}")
                outcomes[goal_id] = Outcome.ERROR

    # Harness-self trend pass — runs ONCE per heartbeat after the per-goal loop.
    # Sentinel goal_id keeps the trace events in the same table for replay via
    # get_trace; the detector observes devclaw itself, not any specific goal.
    if trend_detector is not None:
        harness_tracer = (
            tracer_factory("_harness_self_") if tracer_factory else None
        )
        try:
            with _trace.tracer_scope(harness_tracer):
                await trend_detector.run_harness_self()
        except Exception:  # noqa: BLE001 — telemetry must not break the heartbeat
            pass

    return outcomes


def _engine_pause(engine: GoalEngine) -> tuple[int, str]:
    """Read the shared quota pause via the engine, if it exposes one (the
    in-process engine does; test doubles may not → treated as no pause)."""
    fn = getattr(engine, "global_pause", None)
    return fn() if callable(fn) else (0, "")


def _engine_prune_traces(engine: GoalEngine) -> None:
    """Run the daily trace-retention prune via the engine, if it exposes one
    (the in-process engine does; test doubles may not → no prune). Best-effort:
    a maintenance failure must never break the heartbeat — the traces table
    just stays bigger until a later tick succeeds."""
    fn = getattr(engine, "prune_traces", None)
    if not callable(fn):
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 — maintenance must not break the heartbeat
        pass


def _engine_prune_events(engine: GoalEngine) -> None:
    """Run the daily events-retention prune via the engine, if it exposes one
    (the in-process engine does; test doubles may not → no prune). Best-effort:
    a maintenance failure must never break the heartbeat — the events table
    just stays bigger until a later tick succeeds."""
    fn = getattr(engine, "prune_events", None)
    if not callable(fn):
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 — maintenance must not break the heartbeat
        pass


def _engine_vacuum(engine: GoalEngine) -> None:
    """Run the weekly, freelist-gated VACUUM via the engine, if it exposes one
    (the in-process engine does; test doubles may not → no vacuum). Best-effort:
    a maintenance failure must never break the heartbeat — the .db just stays at
    its current size until a later tick reclaims it."""
    fn = getattr(engine, "vacuum", None)
    if not callable(fn):
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 — maintenance must not break the heartbeat
        pass


async def _maybe_alert_db_size(
    engine: GoalEngine, notifier: Notifier, *, triage_caller: "ClaudeCaller | None" = None,
) -> None:
    """Check the DB-size alarm via the engine and, if it just crossed the
    threshold, ping the owner ONCE. Best-effort on both legs: a stat failure or
    a notifier outage must never break the heartbeat.

    Zero-token idle guard: ``check_db_size_alert`` returns a message ONLY on the
    tick the .db crosses the threshold (deduped by the ``db_size_alerted`` meta
    flag). On every idle / under-threshold tick it returns ``None`` and this
    function returns before any cognition — so the guarantee holds regardless of
    whether triage is wired.

    When ``triage_caller`` is set (production, via GoalService), the alert routes
    through the propose-only self-triage interceptor (:func:`triaged_notify`,
    ``kind="db_size"``): it dedupes against the ``problems`` catalog and proposes
    a grounded retention fix, delivering "problem + proposed fix + how to
    approve" instead of the bare alert. ``triage_caller=None`` (the default, and
    every existing test) keeps the RAW owner send, byte-identical to before. A
    triage failure falls back to the raw alert — loud, not silent."""
    fn = getattr(engine, "check_db_size_alert", None)
    if not callable(fn):
        return
    try:
        msg = fn()
    except Exception:  # noqa: BLE001 — maintenance must not break the heartbeat
        return
    if not msg:
        return
    if triage_caller is None:
        await _notify(notifier, NotifyLevel.OWNER, msg)
        return
    # A real problem fired — enrich it. Catalog + size read through the engine
    # seam (never the StateStore directly); both best-effort so a hiccup degrades
    # to the raw ping rather than swallowing the alarm.
    catalog = ""
    size_bytes = 0
    try:
        lp = getattr(engine, "list_problems", None)
        if callable(lp):
            catalog = _triage.format_catalog(lp())
        sb = getattr(engine, "db_size_bytes", None)
        if callable(sb):
            size_bytes = sb()
    except Exception:  # noqa: BLE001 — grounding is best-effort
        catalog, size_bytes = "", 0
    await triaged_notify(
        notifier, NotifyLevel.OWNER, msg,
        kind="db_size", triage_caller=triage_caller,
        catalog=catalog, repo_context=_triage.retention_context(size_bytes),
    )


def _engine_clear_pause(engine: GoalEngine) -> None:
    fn = getattr(engine, "clear_global_pause", None)
    if callable(fn):
        fn()


def _engine_pause_notified(engine: GoalEngine) -> bool:
    """Read the owner-was-pinged-about-this-pause flag via the engine, if it
    exposes one (the in-process engine does; test doubles may not → False)."""
    fn = getattr(engine, "pause_notified", None)
    return bool(fn()) if callable(fn) else False


def _engine_set_pause_notified(engine: GoalEngine, on: bool) -> None:
    fn = getattr(engine, "set_pause_notified", None)
    if callable(fn):
        fn(on)


def _engine_operator_block(engine: GoalEngine) -> tuple[bool, str]:
    """Read the operator hold + run-window gate via the engine, if it exposes one
    (the in-process engine does; test doubles may not → treated as open)."""
    fn = getattr(engine, "operator_block", None)
    return fn(_now_ms()) if callable(fn) else (False, "")


def _engine_goal_operator_block(engine: GoalEngine, goal_id: str) -> tuple[bool, str]:
    """Read one goal's OWN run-window gate via the engine, if it exposes one (the
    in-process engine does; test doubles may not → treated as open, so existing
    fakes tick every goal exactly as before)."""
    fn = getattr(engine, "goal_operator_block", None)
    return fn(goal_id, _now_ms()) if callable(fn) else (False, "")


def _maybe_pause(engine: GoalEngine, store: GoalStore, goal_id: str, err: str) -> "Outcome | None":
    """If ``err`` is a usage/rate-limit, set the shared quota pause and return
    Outcome.RATE_LIMITED; otherwise None (the caller handles it as a real error).
    Centralizes the goal-side quota guard so every cognition call can use it."""
    # now_utc lets absolute reset wording ("resets 10pm (UTC)") become a real
    # hint; a stated hint is trusted past the default cap (pause_seconds).
    cls = classify_failure(err, now_utc=datetime.now(timezone.utc))
    if not (cls.is_pausing and hasattr(engine, "set_global_pause")):
        return None
    backoff = pause_seconds(cls.retry_after_s, stated=cls.stated)
    engine.set_global_pause(_now_ms() + backoff * 1000, f"{cls.kind.value} (goal cognition)")
    store.append_log(goal_id, f"paused — {cls.kind.value}; resuming in ~{backoff}s")
    return Outcome.RATE_LIMITED
