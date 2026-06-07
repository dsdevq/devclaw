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

import os
from dataclasses import replace
from enum import Enum
from typing import Awaitable, Callable

from . import goal_evaluator as _evaluator
from . import goal_planner as _planner
from .goal_engine import GoalEngine
from .goal_models import Action, EvalResult, Goal, GoalStatus
from .goal_notify import Notifier
from .goal_planner import ClaudeCaller
from .goal_store import GoalStore
from .workspace import WorkspaceError, prepare_workspace

#: (workspace_dir, repo_url) -> default branch. Injected so tests pass a no-op.
WorkspacePrep = Callable[[str, "str | None"], Awaitable[str]]

#: deliveries between periodic direction evaluations (0 → only at the done-gate)
EVAL_EVERY = int(os.environ.get("DEVCLAW_GOAL_EVAL_EVERY", "3"))
#: when True, a planner "done" proposal dispatches a read-only review of the repo
#: against done_when and the evaluator judges THAT before the goal closes.
VERIFY_DONE = os.environ.get("DEVCLAW_GOAL_VERIFY_DONE", "1") not in ("0", "false", "")


class Outcome(str, Enum):
    IDLE = "idle"            # cheap check found nothing — 0 tokens
    IN_FLIGHT = "in_flight"  # dispatched action still running — 0 tokens
    DISPATCHED = "dispatched"
    VERIFYING = "verifying"  # done-gate review dispatched
    SLEPT = "slept"
    BLOCKED = "blocked"
    DONE = "done"
    SKIP_DONE = "skip_done"
    ERROR = "error"


class NotifyLevel(int, Enum):
    """How loud a goal-layer notification is. The owner is a non-technical
    product owner who should hear only OWNER-level events (a real blocker, a
    direction question, a paused-for-review, a verified completion). TASK-level
    is mechanical/internal chatter — per-action dispatch, course-corrections the
    layer handles itself, technical hiccups it retries — surfaced only when
    DEVCLAW_NOTIFY_ALTITUDE=task (debugging). Mechanism, zero tokens."""

    TASK = 0
    OWNER = 1


_ALTITUDES = {"task": NotifyLevel.TASK, "owner": NotifyLevel.OWNER}


def _notify_floor() -> NotifyLevel:
    """The lowest level that still reaches the owner. Default OWNER — only
    owner-altitude events go out; set DEVCLAW_NOTIFY_ALTITUDE=task for the full
    firehose. Read from env each call so it's overridable per process / in tests."""
    return _ALTITUDES.get(
        os.environ.get("DEVCLAW_NOTIFY_ALTITUDE", "owner").strip().lower(), NotifyLevel.OWNER
    )


async def _notify(notifier: Notifier, level: NotifyLevel, text: str) -> None:
    """Send a notification only if it's at/above the configured altitude floor.
    Best-effort (the notifier itself never raises); mechanism — zero tokens."""
    if level >= _notify_floor():
        await notifier.send(text)


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
) -> Outcome:
    goal = store.load_goal(goal_id)
    status = store.load_status(goal_id)
    if status.phase == "done":
        return Outcome.SKIP_DONE

    # ---- cheap check (zero tokens): poll any in-flight action ---------------
    finished_detail = ""
    done_check_detail: str | None = None
    if status.in_flight is not None:
        ref = status.in_flight
        poll = await engine.poll(ref)
        if poll.running:
            store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
            return Outcome.IN_FLIGHT
        # terminal — branch on what kind of action it was.
        evidence = []
        if poll.pr_url:
            evidence.append(f"PR {poll.pr_url}")
        if poll.gate_passed is not None:
            evidence.append("gate=passed" if poll.gate_passed else "gate=FAILED")
        ev_str = (" — " + ", ".join(evidence)) if evidence else ""
        if ref.is_done_check:
            store.append_log(goal_id, f"done-check review {ref.id} → {poll.status}")
            done_check_detail = poll.detail or f"review {poll.status} (no report captured)"
            status = replace(status, in_flight=None, phase="idle")
        else:
            store.append_log(goal_id, f"{ref.tool} {ref.id} → {poll.status}{ev_str}")
            # Grounded evidence for the evaluator — the agent's own output + gate.
            store.append_delivery(goal_id, ref.goal or ref.tool, poll.detail or "")
            finished_detail = (
                f"tool={ref.tool} id={ref.id} status={poll.status}{ev_str}\n{poll.detail}"
            )
            delivered = 1 if poll.status == "done" else 0
            status = replace(
                status, in_flight=None, phase="idle",
                deliveries_since_eval=status.deliveries_since_eval + delivered,
            )

    # ---- DONE-GATE resolution: a verifying review just finished -------------
    if done_check_detail is not None:
        return await _resolve_done_gate(
            goal_id, goal, status, done_check_detail,
            store=store, evaluator_caller=evaluator_caller, notifier=notifier,
        )

    # ---- should we spend cognition at all? (preserve the zero-token guard) --
    steering = store.unread_steering(goal_id, status)
    work = bool(finished_detail) or bool(steering)
    if status.phase == "blocked":
        should_plan = work  # cadence does NOT re-poke a blocked goal; only work unblocks
    else:
        should_plan = work or store.cadence_due(goal, status)
    if not should_plan:
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        return Outcome.IDLE

    # ---- periodic direction eval (mid-flight) ------------------------------
    # Past the gate, and only when enough has shipped, judge direction from the
    # grounded deliveries. Corrections become steering; a hard verdict blocks.
    if eval_every > 0 and status.deliveries_since_eval >= eval_every:
        blocked = await _run_mid_flight_eval(
            goal_id, goal, status,
            store=store, evaluator_caller=evaluator_caller, notifier=notifier,
        )
        status = store.load_status(goal_id)  # eval may have written status + steering
        if blocked is not None:
            return blocked
        steering = store.unread_steering(goal_id, status)  # re-read: eval may have steered

    # ---- next-action plan (cognition) + act --------------------------------
    try:
        result = await _planner.plan(
            goal, status, store.recent_log(goal_id), steering, finished_detail,
            claude_caller=planner_caller,
        )
    except _planner.GoalPlannerError as exc:
        store.append_log(goal_id, f"plan error: {exc}")
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] plan step failed: {exc}")
        return Outcome.ERROR

    now = store.now_iso()
    base = replace(
        status, last_plan_at=now, last_tick_at=now,
        inbox_cursor=store.steering_cursor(goal_id),  # all current steering consumed
    )

    if result.decision == "sleep":
        store.save_status(goal_id, replace(base, phase="idle", next=result.note))
        store.append_log(goal_id, f"sleep: {result.note}")
        return Outcome.SLEPT

    if result.decision == "blocked":
        store.save_status(goal_id, replace(base, phase="blocked", blocked_on=result.question, next=""))
        store.append_log(goal_id, f"blocked: {result.question}")
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] needs you — {result.question}")
        return Outcome.BLOCKED

    if result.decision == "done":
        return await _open_done_gate(
            goal_id, goal, base,
            store=store, engine=engine, evaluator_caller=evaluator_caller,
            notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
            verify_done=verify_done, note=result.note,
        )

    # decision == "act"
    return await _dispatch_action(
        goal_id, goal, base, result.actions[0],
        store=store, engine=engine, notifier=notifier,
        notify_url=notify_url, prepare_ws=prepare_ws,
    )


# ---- evaluation helpers ----------------------------------------------------


def _apply_corrections(store: GoalStore, goal_id: str, ev: EvalResult) -> None:
    if ev.corrections:
        store.append_steering(goal_id, ev.corrections, source="auto-eval")


async def _run_mid_flight_eval(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, evaluator_caller: ClaudeCaller, notifier: Notifier,
) -> "Outcome | None":
    """Periodic, artifact-grounded direction check. Returns an Outcome to return
    early (blocked) or None to keep going. Resets the delivery counter."""
    try:
        ev = await _evaluator.evaluate(
            goal, status, store.recent_log(goal_id), store.recent_deliveries(goal_id),
            claude_caller=evaluator_caller,
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
        store.save_status(goal_id, replace(base, phase="blocked", blocked_on=q, next=""))
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] direction check ({ev.verdict}) — {q}")
        return Outcome.BLOCKED
    store.save_status(goal_id, base)
    _apply_corrections(store, goal_id, ev)
    if ev.verdict == "off_track" and ev.corrections:
        await _notify(notifier, NotifyLevel.TASK, f"🧭 [{goal_id}] course-correcting — {ev.rationale[:200]}")
    return None


async def _resolve_done_gate(
    goal_id: str, goal: Goal, status: GoalStatus, review_report: str,
    *, store: GoalStore, evaluator_caller: ClaudeCaller, notifier: Notifier,
) -> Outcome:
    """A done-gate review just finished — judge the repo against done_when. Only
    'achieved' closes the goal; otherwise corrections are steered back in and the
    goal continues (its next tick plans the next step)."""
    try:
        ev = await _evaluator.evaluate(
            goal, status, store.recent_log(goal_id), store.recent_deliveries(goal_id),
            claude_caller=evaluator_caller, review_report=review_report, at_done_gate=True,
        )
    except _evaluator.GoalEvalError as exc:
        store.append_log(goal_id, f"done-gate eval error: {exc}")
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] done-gate eval failed: {exc}")
        return Outcome.ERROR
    now = store.now_iso()
    base = replace(
        status, last_eval_verdict=ev.verdict, last_eval_at=now,
        last_eval_note=ev.rationale[:300], deliveries_since_eval=0, last_tick_at=now,
    )
    store.append_log(goal_id, f"done-gate: {ev.verdict} — {ev.rationale[:200]}")
    if ev.verdict == "achieved":
        store.save_status(goal_id, replace(base, phase="done", next=ev.rationale[:200]))
        await _notify(notifier, NotifyLevel.OWNER, f"✅ [{goal_id}] goal complete (verified) — {ev.rationale[:200]}")
        return Outcome.DONE
    if ev.verdict in ("stalled", "needs_human"):
        q = ev.question or ev.rationale or "done-gate flagged a problem"
        store.save_status(goal_id, replace(base, phase="blocked", blocked_on=q, next=""))
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] not done — {q}")
        return Outcome.BLOCKED
    # on_track / off_track → not done yet. Steer corrections back in and continue.
    store.save_status(goal_id, replace(base, phase="idle", next="done-gate said keep going"))
    _apply_corrections(store, goal_id, ev)
    await _notify(notifier, NotifyLevel.TASK, f"↩️ [{goal_id}] done-gate: not complete — {ev.rationale[:200]}")
    return Outcome.SLEPT


async def _open_done_gate(
    goal_id: str, goal: Goal, base: GoalStatus,
    *, store: GoalStore, engine: GoalEngine, evaluator_caller: ClaudeCaller,
    notifier: Notifier, notify_url: str, prepare_ws: WorkspacePrep, verify_done: bool,
    note: str,
) -> Outcome:
    """The planner proposed done. Don't trust it: either dispatch a read-only
    review of the repo against done_when (the grounded path) and let the next
    tick judge it, or — if done-verification is disabled — run an artifact-only
    done evaluation now."""
    if verify_done:
        try:
            await prepare_ws(goal.workspace_dir, goal.repo_url)
        except WorkspaceError as exc:
            store.append_log(goal_id, f"done-gate workspace prep failed: {exc}")
            store.save_status(goal_id, replace(base, phase="idle", next="retry done-gate"))
            await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] done-gate workspace prep failed: {exc}")
            return Outcome.ERROR
        review = Action(
            engine="devclaw", tool="review_repository",
            goal=(
                f"Read-only review: does this repository fully satisfy the goal?\n"
                f"Objective: {goal.objective}\nDone when: {goal.done_when}\n"
                f"Report concretely what is satisfied and what (if anything) is missing or wrong."
            ),
            open_pr=False,
        )
        try:
            ref = await engine.dispatch(review, goal, notify_url)
        except Exception as exc:  # noqa: BLE001
            store.append_log(goal_id, f"done-gate dispatch failed: {exc}")
            store.save_status(goal_id, replace(base, phase="idle", next="retry done-gate"))
            await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] done-gate dispatch failed: {exc}")
            return Outcome.ERROR
        ref = replace(ref, is_done_check=True)
        store.save_status(goal_id, replace(base, phase="verifying", in_flight=ref, next="verifying done"))
        store.append_log(goal_id, f"done proposed ({note}) → verifying via review {ref.id}")
        await _notify(notifier, NotifyLevel.TASK, f"🔎 [{goal_id}] looks complete — verifying against done_when")
        return Outcome.VERIFYING
    # verify disabled → artifact-only done evaluation now.
    return await _resolve_done_gate(
        goal_id, goal, base, review_report="",  # no review run; artifact-only
        store=store, evaluator_caller=evaluator_caller, notifier=notifier,
    )


async def _dispatch_action(
    goal_id: str, goal: Goal, base: GoalStatus, action: Action,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep,
) -> Outcome:
    # Runaway backstop (mechanism, not cognition): never spawn more than
    # backlog-size + a small margin of engine actions for one goal without a
    # human. A looping planner can't burn unbounded quota — it blocks instead.
    cap = len(goal.backlog) + 2
    if base.actions_dispatched >= cap:
        store.append_log(goal_id, f"dispatch cap {cap} reached — blocking for review")
        store.save_status(
            goal_id,
            replace(base, phase="blocked", blocked_on=f"dispatch cap {cap} reached — review the open PRs"),
        )
        await _notify(notifier, NotifyLevel.OWNER, f"🛑 [{goal_id}] dispatch cap ({cap}) reached — paused for your review")
        return Outcome.BLOCKED
    # Give the engine a pristine checkout at latest origin/default — so this
    # action doesn't pile onto a previous action's branch (per-action freshness).
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url)
    except WorkspaceError as exc:
        store.append_log(goal_id, f"workspace prep failed: {exc}")
        store.save_status(goal_id, replace(base, phase="idle", next=action.goal))
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] workspace prep failed: {exc}")
        return Outcome.ERROR
    try:
        ref = await engine.dispatch(action, goal, notify_url)
    except Exception as exc:  # noqa: BLE001 — record + notify, retry next cadence
        store.append_log(goal_id, f"dispatch error ({action.tool}): {exc}")
        store.save_status(goal_id, replace(base, phase="idle", next=action.goal))
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] dispatch failed: {exc}")
        return Outcome.ERROR
    store.save_status(
        goal_id,
        replace(
            base, phase="in_flight", in_flight=ref, blocked_on=None, next=action.goal,
            actions_dispatched=base.actions_dispatched + 1,
        ),
    )
    store.append_log(goal_id, f"dispatched {action.tool}: {action.goal} → {ref.id}")
    await _notify(notifier, NotifyLevel.TASK, f"🚀 [{goal_id}] {action.tool}: {action.goal}  ({ref.id})")
    return Outcome.DISPATCHED


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
) -> dict[str, Outcome]:
    """Tick every goal. One goal's failure never stops the others."""
    outcomes: dict[str, Outcome] = {}
    for goal_id in store.list_goal_ids():
        try:
            outcomes[goal_id] = await tick_goal(
                goal_id, store=store, engine=engine,
                planner_caller=planner_caller, evaluator_caller=evaluator_caller,
                notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
                eval_every=eval_every, verify_done=verify_done,
            )
        except Exception:  # noqa: BLE001 — isolate per-goal blast radius
            store.append_log(goal_id, "tick crashed (uncaught)")
            outcomes[goal_id] = Outcome.ERROR
    return outcomes
