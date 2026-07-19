"""Settle & recover in-flight work — the goal-tick polling resolvers.

Where dispatched work comes back: the atomic settle of a regular action (delivery
row + checklist update + auto-merge / program-stack reconcile), the discovery and
done-gate poll resolvers, the checklist-item settle computation, and the
once-per-service-start orphaned-ref sweep. This is the top of the tick_* import
graph — it consumes tick_dispatch (_resolve_discovery) and tick_donegate
(_resolve_done_gate) plus tick_guards + tick_context, and is re-exported from
tick.py (tick._tick_goal_impl chains through _resolve_polling_action).
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Tuple, Union

from .tick_context import (
    NotifyLevel,
    Outcome,
    Phase,
    TickContext,
    _action_label,
    _classify,
    _notify,
)
from .tick_guards import _block_on_lost_ref
from .tick_dispatch import _resolve_discovery
from .tick_donegate import _resolve_done_gate
from . import checklist as _checklist
from . import reconcile as _reconcile
from .engine import GoalEngine, GoalEngineError
from .models import Checklist, Goal, GoalStatus, InFlight, PollResult
from .store import GoalStore
from .transitions import Event
from ..loom import trace as _trace


#: structural per-item circuit breaker (#6): after this many FAILED settles of
#: the SAME checklist item, stop re-picking it — flip it to ``blocked`` and park
#: the goal for a human, instead of the planner spinning the same failing ticket
#: (the closeloop-bench 2026-07-18 pattern where a hand-written "CIRCUIT BREAKER"
#: clause in the task prose was the only — and unreliable — brake on a 4th
#: identical attempt). Mirrors the per-workspace breaker's 3-failure instinct
#: (task_queue._check_and_trip_breaker). Env-overridable; ``<= 0`` disables it.
ITEM_MAX_ATTEMPTS = int(os.environ.get("DEVCLAW_ITEM_MAX_ATTEMPTS", "3"))


def _failure_note(poll: PollResult) -> str:
    """A compact one-liner of what went wrong, for the item's failure_log.
    Prefers the END of poll.detail — the error / gate tail carries the signal;
    the front is the agent's own summary. Whitespace-flattened and bounded so
    N notes stay brief-sized."""
    parts: list[str] = [f"settled {poll.status}"]
    if poll.gate_passed is False:
        parts.append("sandbox gate=FAILED")
    tail = " ".join((poll.detail or "").split())
    if tail:
        parts.append("…" + tail[-260:] if len(tail) > 260 else tail)
    return " · ".join(parts)


def _settle_addressed_items(
    checklist: "Checklist", addresses: list[str], poll: PollResult,
) -> "Checklist":
    """Compute the checklist with the addressed items settled. Successful
    task (poll.status == 'done' AND gate_passed in {None, True}) flips items
    to ``done`` with grounded evidence (PR url + gate verdict) and resets
    ``attempts`` to 0; a failed task increments each addressed item's
    ``attempts`` and flips it back to ``not_started`` so the planner can
    re-pick it next tick — UNTIL it has failed :data:`ITEM_MAX_ATTEMPTS`
    straight times, at which point the structural per-item circuit breaker
    (#6) flips it to ``blocked`` instead (the caller then parks the goal for a
    human). The per-item gate (review_gate) verifies the diff against
    ``evidence_target`` separately — session 4.

    PR7: pure — returns the updated :class:`Checklist` instead of writing it.
    The caller (``_resolve_polling_action``) reads the current checklist,
    calls this to COMPUTE the update, and persists it as a row-only write
    (``write_checklist(..., render_view=False)``) INSIDE the settle
    transaction — so a rolled-back settle (CAS conflict) can't leave an
    item settled for a delivery that was never actually recorded. The "no
    checklist" / "addresses is empty" guards moved to the caller, which now
    decides whether to call this at all."""
    success = poll.status == "done" and (poll.gate_passed is None or poll.gate_passed)
    if success:
        ev_parts: list[str] = []
        if poll.pr_url:
            # Checklist dispatches never auto-merge (the shared goal-branch PR
            # stays open for the owner), so state that in the evidence itself.
            # An unqualified "PR <url> · gate=passed" reads as "merged and
            # green" to every downstream consumer — the closeloop-bench
            # 2026-07-05 run logged "PR merged (gate passed)" for a PR that
            # was never merged because this string let it.
            ev_parts.append(f"PR {poll.pr_url} (unmerged)")
        if poll.gate_passed is not None:
            # devclaw's sandbox verify_cmd, not the target repo's CI.
            ev_parts.append("sandbox gate=passed" if poll.gate_passed else "sandbox gate=FAILED")
        evidence = " · ".join(ev_parts) or "settled (no PR or gate)"
        updated = checklist
        for item_id in addresses:
            try:
                # attempts reset to 0: a proven item carries no stale failure
                # count, so a later steer that re-opens it for rework starts
                # fresh rather than pre-tripping the breaker.
                updated = _checklist.update_item(
                    updated, item_id, status="done", evidence=evidence, attempts=0,
                    clear_failure_log=True,
                )
            except KeyError:
                continue
        return updated

    # Failure: bump each addressed item's attempt count. Below the cap it goes
    # back to ``not_started`` (the pick-pool, evidence left as-is — not yet
    # proven); AT the cap the circuit breaker trips it to ``blocked`` so the
    # planner stops re-picking it and the caller parks the goal for a human.
    by_id = {i.id: i for i in checklist.items}
    updated = checklist
    for item_id in addresses:
        item = by_id.get(item_id)
        if item is None:
            continue
        n = item.attempts + 1
        note = f"attempt {n}: {_failure_note(poll)}"
        if ITEM_MAX_ATTEMPTS > 0 and n >= ITEM_MAX_ATTEMPTS:
            updated = _checklist.update_item(
                updated, item_id, status="blocked", attempts=n,
                failure_note=note,
                evidence=(
                    f"circuit breaker: {n} straight failed attempts — parked "
                    f"for a human decision (steer with a different approach, "
                    f"fix by hand, or re-scope the item)"
                ),
            )
        else:
            # Back to the pick-pool WITH the failure recorded: the dispatch
            # path renders failure_log into the next worker's brief so a
            # re-dispatched item doesn't re-discover a failed approach one
            # attempt at a time (cross-dispatch continuity).
            updated = _checklist.update_item(
                updated, item_id, status="not_started", attempts=n,
                failure_note=note,
            )
    return updated


def _settle_program_items(
    checklist: "Checklist", addresses: list[str], poll: PollResult,
) -> "Checklist":
    """Per-item settle for a PLANNED-PROGRAM ref (one-shot mode, ADR 0003
    stage 2): each addressed checklist item is graded by ITS OWN child task's
    verdict — joined on the task row's ``plan_key``, which the dispatch path
    set to the item id — instead of painting every item with the aggregate
    program status (a one-child failure must not mark the succeeded items
    failed, and vice versa a mostly-failed program must not bury one item
    that shipped). An item whose child is missing from the breakdown (or a
    poll with no breakdown at all — an engine that predates it) falls back to
    the aggregate verdict, exactly the pre-existing behavior. Pure, like
    :func:`_settle_addressed_items`, which it delegates each item to."""
    by_key: dict[str, dict] = {}
    for t in poll.tasks or []:
        if isinstance(t, dict) and t.get("plan_key"):
            by_key[str(t["plan_key"])] = t
    updated = checklist
    for item_id in addresses:
        child = by_key.get(item_id)
        if child is None:
            child_poll = poll  # no per-child verdict — aggregate fallback
        else:
            child_poll = PollResult(
                terminal=True,
                status=str(child.get("status") or ""),
                detail=str(child.get("error") or ""),
                pr_url=child.get("pr_url"),
                gate_passed=child.get("gate_passed"),
            )
        updated = _settle_addressed_items(updated, [item_id], child_poll)
    return updated


async def _resolve_polling_discovery(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> Outcome:
    """Settle an in-flight discovery review. Still running → IN_FLIGHT. Else
    record the review outcome, clear in_flight, and synthesize the brief via
    :func:`_resolve_discovery`.

    PR7 "light settle": record_settlement + the log row + the DISCOVERY_SETTLED
    transition land as ONE transaction; mirrors flush after commit. A
    TransitionConflict rolls the settlement/log/transition back together —
    the retry tick re-polls the same terminal ref and settles cleanly."""
    ref = status.in_flight
    try:
        poll = await ctx.engine.poll(ref)
    except GoalEngineError as exc:
        return await _block_on_lost_ref(goal_id, status, exc, ctx)
    if poll.running:
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        return Outcome.IN_FLIGHT
    discovery_detail = poll.detail or f"review {poll.status} (no analysis captured)"
    try:
        with ctx.store.transaction():
            ctx.store.record_settlement(goal_id, ref_id=ref.id, ref_kind=ref.ref_kind, status=poll.status)
            ctx.store.append_log(goal_id, f"discovery review {ref.id} → {poll.status}", mirror=False)
            # Persist BEFORE the synthesis call (which may raise on a usage
            # limit) so a later crash can't rewind to "still in-flight" and
            # re-poll the same ref. Thread the RETURNED (fresh-versioned)
            # status into _resolve_discovery — its own transition() calls
            # CAS against THIS version, not a stale copy.
            new_status = ctx.store.transition(
                goal_id, Event.DISCOVERY_SETTLED,
                replace(status, in_flight=None, phase="idle"),
                expect=status,
            )
    except Exception:
        ctx.store.discard_pending_mirrors(goal_id)
        raise
    ctx.store.render_mirrors(goal_id)
    return await _resolve_discovery(
        goal_id, goal, new_status, discovery_detail,
        store=ctx.store, research_caller=ctx.evaluator_caller, notifier=ctx.notifier,
        summarize=ctx.summary_caller,
        decompose_enabled=ctx.decompose_enabled,
        decomposer_caller=ctx.decomposer_caller,
    )


async def _resolve_polling_done_gate(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> Outcome:
    """Settle an in-flight done-gate review. Still running → IN_FLIGHT. Else
    record the review outcome, clear in_flight, and judge the repo against
    ``done_when`` via :func:`_resolve_done_gate`. Same PR7 "light settle"
    shape as :func:`_resolve_polling_discovery` — see its docstring."""
    ref = status.in_flight
    try:
        poll = await ctx.engine.poll(ref)
    except GoalEngineError as exc:
        return await _block_on_lost_ref(goal_id, status, exc, ctx)
    if poll.running:
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        return Outcome.IN_FLIGHT
    review_report = poll.detail or f"review {poll.status} (no report captured)"
    try:
        with ctx.store.transaction():
            ctx.store.record_settlement(goal_id, ref_id=ref.id, ref_kind=ref.ref_kind, status=poll.status)
            ctx.store.append_log(goal_id, f"done-check review {ref.id} → {poll.status}", mirror=False)
            new_status = ctx.store.transition(
                goal_id, Event.DONE_GATE_SETTLED,
                replace(status, in_flight=None, phase="idle"),
                expect=status,
            )
    except Exception:
        ctx.store.discard_pending_mirrors(goal_id)
        raise
    ctx.store.render_mirrors(goal_id)
    return await _resolve_done_gate(
        goal_id, goal, new_status, review_report,
        store=ctx.store, evaluator_caller=ctx.evaluator_caller, notifier=ctx.notifier,
        summarize=ctx.summary_caller, remote_checker=ctx.remote_checker,
        autodeploy=ctx.autodeploy,
    )


def _readopt_orphaned_ref(
    goal_id: str, status: GoalStatus, store: GoalStore, engine: GoalEngine,
) -> "str | None":
    """Rediscover + re-adopt ONE goal's lost in-flight ref — a TASK or a
    PROGRAM this goal dispatched whose in-flight ref was lost (STATUS.md
    truncated by a crash mid-write, a restart racing the status write).
    Formerly the per-tick ``_readopt_orphaned_program`` (2026-07-09
    incident); PR7 extends it to tasks and demotes it to a once-per-service-
    start sweep (see :func:`sweep_orphaned_refs`) — atomic dispatch means a
    ref can no longer be lost MID-FLIGHT, so a per-tick check is no longer
    load-bearing; a startup sweep still catches refs lost by an OLDER build,
    or a restart landing in the (now much narrower) commit-to-kick window.

    "Orphan" = the goal's most recent task/program by ``parent_goal_id``
    with no recorded settlement (:meth:`GoalStore.is_settled`, PR7's
    replacement for the old ``log_contains(f" {id} → ")`` string match) —
    running OR already-terminal both qualify; the normal POLLING_ACTION path
    then polls/settles it exactly as if the ref had never been lost.

    Checks the TASK finder first, then the PROGRAM finder: in a healthy
    system ``in_flight`` is a single slot, so a goal essentially never has
    BOTH an orphaned task and an orphaned program at once. When it
    theoretically does, task-first is a pragmatic simplification — comparing
    ``created_at`` precisely would need both finders to expose it, for a
    benefit that in practice never matters (the brief this PR implements
    sanctions this choice explicitly). Engines without a finder (fakes,
    remote) opt out silently via getattr, same as the pre-PR7 program-only
    version.

    Returns a short description of what was re-adopted (``"task <id>"`` /
    ``"program <id>"``), or None if nothing needed re-adopting."""
    task_finder = getattr(engine, "latest_task_for_goal", None)
    if task_finder is not None:
        found_task = task_finder(goal_id)
        if found_task is not None:
            task_id, task_goal, task_kind = found_task
            if not store.is_settled(goal_id, task_id):
                _readopt_ref(store, goal_id, status, ref_id=task_id, ref_kind="task", tool=task_kind, ref_goal=task_goal)
                return f"task {task_id}"
    program_finder = getattr(engine, "latest_program_for_goal", None)
    if program_finder is not None:
        found_program = program_finder(goal_id)
        if found_program is not None:
            program_id, program_goal = found_program
            if not store.is_settled(goal_id, program_id):
                _readopt_ref(store, goal_id, status, ref_id=program_id, ref_kind="program", tool="start_program", ref_goal=program_goal)
                return f"program {program_id}"
    return None


def _readopt_ref(
    store: GoalStore, goal_id: str, status: GoalStatus,
    *, ref_id: str, ref_kind: str, tool: str, ref_goal: str,
) -> None:
    """Write the actual re-adoption: restore ``in_flight`` (DISPATCH_ACTION)
    + a log line, as ONE transaction; mirrors flush after commit. A lost
    done-check/discovery ref is deliberately re-adopted as a PLAIN action ref
    — WITHOUT its ``is_done_check``/``is_discovery`` flag, since that flag
    lived only on the lost ref and cannot be recovered from the task/program
    row alone. This is conservative by construction: the settle just records
    a delivery (instead of re-entering the done-gate/discovery resolution
    path directly), and the planner naturally re-proposes done — or
    investigation resumes on backlog — on its own next tick if warranted."""
    ref = InFlight("devclaw", tool, ref_id, ref_kind, ref_goal)
    try:
        with store.transaction():
            store.transition(
                goal_id, Event.DISPATCH_ACTION,
                replace(status, in_flight=ref, phase="in_flight"),
                expect=status,
            )
            store.append_log(
                goal_id,
                f"re-adopted orphaned {ref_kind} {ref_id} — its in-flight ref was "
                "missing from STATUS.md (lost state, e.g. a restart mid-write); "
                "settling it now instead of waiting on a result that would never arrive",
                mirror=False,
            )
    except Exception:
        store.discard_pending_mirrors(goal_id)
        raise
    store.render_mirrors(goal_id)


async def sweep_orphaned_refs(store: GoalStore, engine: GoalEngine) -> "dict[str, str]":
    """Once-per-service-start sweep: for every goal, re-adopt a lost
    in-flight ref if one is found (see :func:`_readopt_orphaned_ref`).
    Returns ``{goal_id: description}`` for every goal that was re-adopted —
    empty when nothing needed it.

    Guard mirrors the condition the OLD per-tick readopt effectively had:
    EXECUTING classification (terminal/investigating/firming goals, and any
    goal that already has a fresh in_flight ref, are skipped) with no ref.
    A single goal's bad state (a corrupt status row, a raised finder) is
    isolated — logged where possible, never allowed to sink the whole sweep,
    matching ``tick_all``'s per-goal isolation.

    Does NOT take :func:`_tick_lock` (PR8): this runs once, before the
    heartbeat loop starts (see ``GoalService._loop``) — single-threaded at
    that point, nothing else can be ticking any goal yet, so there is no
    same-goal concurrency for the lock to guard against here."""
    result: "dict[str, str]" = {}
    for goal_id in store.list_goal_ids():
        try:
            status = store.load_status(goal_id)
            if status.in_flight is not None:
                continue
            if _classify(status) is not Phase.EXECUTING:
                continue
            outcome = _readopt_orphaned_ref(goal_id, status, store, engine)
        except Exception as exc:  # noqa: BLE001 — one goal's trouble must not sink the sweep
            try:
                store.append_log(goal_id, f"startup sweep error (isolated): {exc}")
            except Exception:  # noqa: BLE001 — even the log write must not propagate
                pass
            continue
        if outcome:
            result[goal_id] = outcome
    return result


async def _resolve_polling_action(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> "Union[Outcome, Tuple[GoalStatus, str]]":
    """Settle an in-flight regular action. Still running → IN_FLIGHT.
    Otherwise: record the delivery (grounded evidence for the evaluator),
    update the no-progress watchdog, and commit the settlement + delivery +
    log + checklist rows + the ACTION_SETTLED transition as ONE transaction
    (PR7) — protects against the duplicate-merge loop dogfooded 2026-06-21
    AND closes a PR4-review nuance: a TransitionConflict landing in this
    window now rolls EVERYTHING back (no partial artifacts, no duplicate log
    line), where before only the transition itself was guarded. Auto-merge /
    program-reconcile move to AFTER the commit — see the comment at that
    call site for the observable-order note. Returns ``(new_status,
    finished_detail)`` so the EXECUTING handler can plan the next action on
    the same tick with the just-finished detail in hand."""
    ref = status.in_flight
    try:
        poll = await ctx.engine.poll(ref)
    except GoalEngineError as exc:
        return await _block_on_lost_ref(goal_id, status, exc, ctx)
    if poll.running:
        ctx.store.update_status_fields(goal_id, last_tick_at=ctx.store.now_iso())
        return Outcome.IN_FLIGHT

    # ---- compute everything the settle transaction will write, BEFORE ------
    # ---- opening it (no cognition, no I/O below — pure computation) --------
    evidence = []
    if poll.pr_url:
        evidence.append(f"PR {poll.pr_url}")
    if poll.gate_passed is not None:
        # Say WHICH gate: devclaw's sandbox verify_cmd, not the target repo's
        # CI. The bare "gate=passed" wording let the closeloop-bench 2026-07-05
        # planner treat sandbox-green as CI-green while every real GitHub
        # Actions run was failing at startup.
        evidence.append("sandbox gate=passed" if poll.gate_passed else "sandbox gate=FAILED")
    ev_str = (" — " + ", ".join(evidence)) if evidence else ""
    settle_line = f"{ref.tool} {ref.id} → {poll.status}{ev_str}"

    # Checklist mode: settle the items this action was addressing — success
    # flips them to done with grounded evidence (PR + gate), failure flips
    # them back to not_started so the planner can re-pick them. Pure compute
    # here (PR7); the caller persists the result row-only, inside the txn.
    addresses = list(getattr(ref, "addresses", None) or [])
    updated_checklist = None
    if addresses:
        current_checklist = ctx.store.read_checklist(goal_id)
        if current_checklist is not None:
            if ref.ref_kind == "program" and poll.tasks and goal.mode == "one_shot":
                # One-shot planned program: grade each item by its own child —
                # the dispatch path guaranteed plan_key == item id. Scoped to
                # one_shot ON PURPOSE: a long-lived goal's program children are
                # planned by the queue's decomposer, whose slug-style keys can
                # accidentally collide with checklist item ids — an accidental
                # join must not flip a milestone item to done off a partial
                # program (the pre-existing aggregate verdict stays authoritative
                # there).
                updated_checklist = _settle_program_items(current_checklist, addresses, poll)
            else:
                updated_checklist = _settle_addressed_items(current_checklist, addresses, poll)

    delivered = 1 if poll.status == "done" else 0
    # Any SUCCESSFUL settle hands back its dispatch-cap budget: the cap exists
    # to stop a planner that spins without producing, not to ration healthy
    # throughput. That includes gateless settles (reviews, programs) — a
    # mission goal that grounds every delivery in a read-only verification
    # review was structurally re-tripping the cap every ~6 cycles while every
    # verdict was on_track (live-found 2026-07-09, closeloop-mission-v2, one
    # night after the #172 refund shipped). Only failures and gate-FAILED work
    # accumulate; churn on successful-but-aimless dispatches is the direction
    # evaluator's and no-progress watchdog's job, not this counter's.
    productive = 1 if (poll.status == "done" and poll.gate_passed is not False) else 0
    new_status = replace(
        status, in_flight=None, phase="idle",
        deliveries_since_eval=status.deliveries_since_eval + delivered,
        actions_dispatched=max(0, status.actions_dispatched - productive),
        # A productive settle also earns the mechanical auto-heal budget back
        # (tick_guards._autoheal_corrupt_doc) — the SAME stability signal as
        # the cap refund above, riding the same ACTION_SETTLED write (no extra
        # write, atomic with the settle): a goal that ships real work again is
        # stable, so a later mechanical block starts with a fresh heal budget
        # instead of a stale flap count from a long-resolved incident.
        heal_attempts=(0 if productive else status.heal_attempts),
        # a delivery is forward progress → reset the no-progress watchdog.
        last_progress_at=(ctx.store.now_iso() if delivered else status.last_progress_at),
        no_progress_notified=(False if delivered else status.no_progress_notified),
    )

    # ---- the atomic settle ---------------------------------------------
    # settlement row + delivery row + log row + checklist update + the
    # ACTION_SETTLED transition, as ONE unit. A TransitionConflict here rolls
    # ALL of it back — settlement, delivery, log, checklist — so the retry
    # tick re-settles this same terminal ref identically: no partial
    # artifacts, no duplicate log line. ref_id=ref.id on record_settlement +
    # append_delivery is the idempotency key (PR6/PR7): a retry re-running
    # this settle for the same ref is a no-op INSERT, not a duplicate.
    try:
        with ctx.store.transaction():
            ctx.store.record_settlement(goal_id, ref_id=ref.id, ref_kind=ref.ref_kind, status=poll.status)
            ctx.store.append_delivery(goal_id, ref.goal or ref.tool, poll.detail or "", ref_id=ref.id, mirror=False)
            ctx.store.append_log(goal_id, settle_line, mirror=False)
            if updated_checklist is not None:
                ctx.store.write_checklist(goal_id, updated_checklist, render_view=False)
            # Persist IMMEDIATELY (within this same atomic unit) — the
            # next-action planner can raise on a usage limit; if the cleared
            # state isn't durable first the tick aborts with in_flight still
            # pointing at the just-finished action and the next tick
            # re-ships it (duplicate-merge loop, dogfood 2026-06-21). Thread
            # the RETURNED (fresh-versioned) status onward — _handle_executing's
            # `expect=` calls CAS against THIS version, not the pre-settle
            # snapshot.
            new_status = ctx.store.transition(goal_id, Event.ACTION_SETTLED, new_status, expect=status)
    except Exception:
        ctx.store.discard_pending_mirrors(goal_id)
        raise
    ctx.store.render_mirrors(goal_id)

    _trace.record_delivery(
        goal_id=goal_id, action_label=_action_label(ref),
        gate_passed=poll.gate_passed, pr_url=poll.pr_url or "",
        diff_stats=poll.diff_stats,
    )

    # ---- structural per-item circuit breaker (#6) --------------------------
    # If this failed settle just tripped an addressed item to ``blocked``
    # (ITEM_MAX_ATTEMPTS straight failures — see _settle_addressed_items), the
    # planner must stop re-picking it: park the whole goal for a human with a
    # named OWNER ping, rather than spinning the same ticket. This is the
    # STRUCTURAL replacement for the planner-authored "CIRCUIT BREAKER" prose
    # that was the only brake in the closeloop-bench 2026-07-18 run — and that
    # a forgetful planner sometimes never wrote (the parallel closeloop
    # deletion loop got three drift-accumulating attempts with no clause). The
    # block rides a SEPARATE transition after the settle committed, CAS'd
    # against the just-written status; auto-merge below is skipped anyway on a
    # non-``done`` poll, so ordering is safe.
    if updated_checklist is not None:
        tripped = [
            i.id for i in updated_checklist.items
            if i.id in addresses and i.status == "blocked"
        ]
        if tripped:
            reason = (
                f"circuit breaker: checklist item(s) {', '.join(tripped)} failed "
                f"{ITEM_MAX_ATTEMPTS} straight attempts — parked for your decision. "
                f"Steer a different approach, fix by hand, or re-scope the item(s)."
            )
            ctx.store.transition(
                goal_id, Event.BLOCK,
                replace(new_status, phase="blocked", blocked_on=reason,
                        blocked_kind="needs_answer"),
                expect=new_status,
            )
            ctx.store.append_log(goal_id, reason)
            await _notify(
                ctx.notifier, NotifyLevel.OWNER, f"🛑 [{goal_id}] {reason}",
                summarize=ctx.summary_caller,
            )
            return Outcome.BLOCKED

    # ---- post-commit tail: auto-merge / program-reconcile (real awaits) ----
    # Moved here (from before the status write, pre-PR7) so both now run
    # STRICTLY AFTER the settle has committed — shrinking the 2026-06-21
    # duplicate-merge window and killing the remaining conflict-retry
    # artifacts. Two observable differences from pre-PR7, both intentional:
    # (a) the "auto-merged …" / "reconcile: …" log lines now land AFTER the
    # settle line in log.md (same content, slightly different file order);
    # (b) a crash after commit but before the merge attempt now leaves the
    # task settled-but-unmerged (a PR left for review — the SAFE direction:
    # pre-PR7, a crash there lost the settle too and the whole thing re-ran).
    #
    # Hands-off auto-merge: a delivered change whose verify gate passed is
    # merged by devclaw itself, with a plain owner ping. Best-effort + gated —
    # a failed merge just leaves the PR for review.
    #
    # ``ctx.merger`` being non-None IS the enabled decision — GoalService
    # resolves it per-goal (project automerge override, else the devclaw-wide
    # DEVCLAW_GOAL_AUTOMERGE default; see devclaw.goal.merge.resolve_automerge)
    # BEFORE it ever reaches this tick. This function must not re-check the
    # raw global flag itself — doing so would mean a project's explicit
    # override could never turn merging ON when the fleet-wide default is off
    # (or off when the default is on), defeating the whole point of a
    # per-project override.
    #
    # Pillar 2 exception: when this action was a checklist-mode dispatch
    # (action carries ``addresses`` of one or more checklist items), the PR
    # is the SHARED goal-branch PR that subsequent items will keep pushing
    # to. Auto-merging it now deletes the goal branch and forces item N+1
    # to re-fork from main, fanning out into a new PR (the 2026-06-26
    # finance-sentry-mcp-v4 regression that broke the v3 rerun's "one PR
    # per goal" guarantee on item 1). Skip auto-merge in that case — the
    # done-gate is the natural moment for a single human review of the
    # cumulative work.
    in_checklist_dispatch = bool(addresses)
    merged_now = False
    if (
        ctx.merger is not None
        and poll.status == "done" and poll.gate_passed and poll.pr_url
        and not in_checklist_dispatch
    ):
        if await ctx.merger(poll.pr_url):
            merged_now = True
            ctx.store.append_log(goal_id, f"auto-merged {poll.pr_url}")
            await _notify(
                ctx.notifier, NotifyLevel.TASK,
                f"✅ [{goal_id}] shipped + merged — {_action_label(ref)} ({poll.pr_url})",
                summarize=ctx.summary_caller,
            )
        else:
            ctx.store.append_log(goal_id, f"auto-merge failed, left for review: {poll.pr_url}")
            # Loud, not silent (2026-07-17): automerge is ENABLED for this goal
            # but the merge did not land (failing/pending checks, a conflict, a gh
            # hiccup). The best-effort merger swallows the reason and returns
            # False, so WITHOUT this the owner never learns it was attempted — and
            # is later paged to "please merge PR X" as if nothing tried (the
            # finance-sentry "automerge never fired" confusion, 2026-07-17). A PR
            # that needs a manual merge IS a needs-you event → OWNER altitude.
            await _notify(
                ctx.notifier, NotifyLevel.OWNER,
                f"⚠️ [{goal_id}] auto-merge failed — {_action_label(ref)} shipped "
                f"but its PR did not merge automatically (check its CI/mergeability). "
                f"Please merge it by hand: {poll.pr_url}",
                summarize=ctx.summary_caller,
            )

    # Program settle: a finished program leaves a STACK of PRs the single-PR
    # auto-merge above can't touch (no single gate verdict). Reconcile the
    # stack mechanically — close superseded, merge green in order, leave red
    # with a reason — so the goal stops burning follow-up dispatches
    # shepherding its own PRs to main and stops leaving zombies behind
    # (live-found 2026-07-09: five open superseded closeloop PRs). Same
    # merger gate as auto-merge: no merger resolved → owner reviews by hand.
    reconcile_summary: list[str] = []
    if (
        ctx.merger is not None
        and ref.ref_kind == "program" and poll.status == "done" and poll.pr_url
    ):
        stack = [u.strip() for u in poll.pr_url.split(";") if u.strip()]
        reconcile_summary = await _reconcile.reconcile_stack(
            stack, workspace_dir=goal.workspace_dir, merger=ctx.merger,
        )
        for line in reconcile_summary:
            ctx.store.append_log(goal_id, f"reconcile: {line}")

    # Built AFTER the auto-merge attempt so the planner is told the PR's real
    # state instead of inferring it. "open (unmerged — owner review pending)"
    # is the closeloop-bench 2026-07-05 fix: the planner's done-proposal prose
    # claimed "PR merged (gate passed)" for a PR nothing had merged, because
    # the detail string never said otherwise.
    pr_state = ""
    if reconcile_summary:
        pr_state = " pr_stack reconciled:\n" + "\n".join(f"  - {line}" for line in reconcile_summary)
    elif poll.pr_url:
        pr_state = (
            " pr_state=merged" if merged_now
            else " pr_state=open (unmerged — owner review pending)"
        )
    finished_detail = f"tool={ref.tool} id={ref.id} status={poll.status}{ev_str}{pr_state}\n{poll.detail}"

    return new_status, finished_detail
