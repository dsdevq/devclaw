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
from . import goal_grill as _grill
from . import goal_merge as _merge
from . import goal_planner as _planner
from . import goal_research as _research
from . import goal_summary as _goal_summary
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
    ASKED = "asked"          # emitted a grill question / plan for approval — awaiting the owner


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


async def _notify(
    notifier: Notifier, level: NotifyLevel, text: str,
    *, summarize: "ClaudeCaller | None" = None,
) -> None:
    """Send a notification only if it's at/above the configured altitude floor.
    When a ``summarize`` caller is supplied, OWNER-level messages are first
    rewritten into plain language for a non-technical owner (best-effort — the
    summarizer never loses or blocks a notification). The altitude gate itself
    is mechanism (zero tokens); cognition runs only for owner-facing sends that
    actually clear the gate, never on the idle path."""
    if level < _notify_floor():
        return
    if summarize is not None and level >= NotifyLevel.OWNER:
        text = await _goal_summary.plain_summary(text, caller=summarize)
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
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    grill_caller: "ClaudeCaller | None" = None,
) -> Outcome:
    goal = store.load_goal(goal_id)
    status = store.load_status(goal_id)
    if status.phase == "done":
        return Outcome.SKIP_DONE
    #: None on a legacy goal → it behaves as "executing" (flat backlog), never
    #: entering the planning front-end. New outcome goals start at "new".
    lifecycle = status.lifecycle or "executing"

    # ---- cheap check (zero tokens): poll any in-flight action ---------------
    finished_detail = ""
    done_check_detail: str | None = None
    discovery_detail: str | None = None
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
        if ref.is_discovery:
            store.append_log(goal_id, f"discovery review {ref.id} → {poll.status}")
            discovery_detail = poll.detail or f"review {poll.status} (no analysis captured)"
            status = replace(status, in_flight=None, phase="idle")
        elif ref.is_done_check:
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
            # Hands-off auto-merge (decision 2): a delivered change whose verify
            # gate passed is merged by devclaw itself, with a plain owner ping.
            # Best-effort + gated — a failed merge just leaves the PR for review.
            if (
                _merge.AUTOMERGE_ENABLED and merger is not None
                and poll.status == "done" and poll.gate_passed and poll.pr_url
            ):
                if await merger(poll.pr_url):
                    store.append_log(goal_id, f"auto-merged {poll.pr_url}")
                    await _notify(
                        notifier, NotifyLevel.OWNER,
                        f"✅ [{goal_id}] shipped + merged — {ref.goal or ref.tool} ({poll.pr_url})",
                        summarize=summary_caller,
                    )
                else:
                    store.append_log(goal_id, f"auto-merge failed, left for review: {poll.pr_url}")

    # ---- DISCOVERY resolution: the investigating review just finished -------
    if discovery_detail is not None:
        return await _resolve_discovery(
            goal_id, goal, status, discovery_detail,
            store=store, research_caller=evaluator_caller, notifier=notifier,
            summarize=summary_caller, grill_caller=grill_caller,
        )

    # ---- DONE-GATE resolution: a verifying review just finished -------------
    if done_check_detail is not None:
        return await _resolve_done_gate(
            goal_id, goal, status, done_check_detail,
            store=store, evaluator_caller=evaluator_caller, notifier=notifier,
            summarize=summary_caller,
        )

    # ---- LIFECYCLE: a new outcome goal investigates before it executes ------
    # (grilling / plan_review are filled in by later build steps; today the
    #  investigating phase flows straight to executing once the brief is written.)
    if lifecycle == "new":
        return await _open_investigation(
            goal_id, goal, status,
            store=store, engine=engine, notifier=notifier,
            notify_url=notify_url, prepare_ws=prepare_ws, summarize=summary_caller,
        )

    # ---- GRILLING: align on scope, one question at a time over Telegram -----
    if lifecycle == "grilling":
        return await _run_grill(
            goal_id, goal, status,
            store=store, grill_caller=grill_caller, notifier=notifier, summarize=summary_caller,
        )

    # ---- PLAN_REVIEW: the spec is agreed; wait for the owner's approval -----
    if lifecycle == "plan_review":
        if store.plan_approved(goal_id):
            store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle", next="plan approved → executing"))
            store.append_log(goal_id, "plan approved → executing")
            await _notify(notifier, NotifyLevel.OWNER, f"✅ [{goal_id}] plan approved — starting work now", summarize=summary_caller)
            return Outcome.SLEPT
        # waiting for approval → zero tokens
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        return Outcome.IDLE

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
            summarize=summary_caller,
        )
        status = store.load_status(goal_id)  # eval may have written status + steering
        if blocked is not None:
            return blocked
        steering = store.unread_steering(goal_id, status)  # re-read: eval may have steered

    # ---- next-action plan (cognition) + act --------------------------------
    try:
        result = await _planner.plan(
            goal, status, store.recent_log(goal_id), steering, finished_detail,
            claude_caller=planner_caller, discovery=store.read_discovery(goal_id),
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
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] needs you — {result.question}", summarize=summary_caller)
        return Outcome.BLOCKED

    if result.decision == "done":
        return await _open_done_gate(
            goal_id, goal, base,
            store=store, engine=engine, evaluator_caller=evaluator_caller,
            notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
            verify_done=verify_done, note=result.note, summarize=summary_caller,
        )

    # decision == "act"
    return await _dispatch_action(
        goal_id, goal, base, result.actions[0],
        store=store, engine=engine, notifier=notifier,
        notify_url=notify_url, prepare_ws=prepare_ws, summarize=summary_caller,
    )


# ---- evaluation helpers ----------------------------------------------------


def _apply_corrections(store: GoalStore, goal_id: str, ev: EvalResult) -> None:
    if ev.corrections:
        store.append_steering(goal_id, ev.corrections, source="auto-eval")


async def _run_mid_flight_eval(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, evaluator_caller: ClaudeCaller, notifier: Notifier,
    summarize: "ClaudeCaller | None" = None,
) -> "Outcome | None":
    """Periodic, artifact-grounded direction check. Returns an Outcome to return
    early (blocked) or None to keep going. Resets the delivery counter."""
    try:
        ev = await _evaluator.evaluate(
            goal, status, store.recent_log(goal_id), store.recent_deliveries(goal_id),
            claude_caller=evaluator_caller, spec=store.read_spec(goal_id),
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
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] direction check ({ev.verdict}) — {q}", summarize=summarize)
        return Outcome.BLOCKED
    store.save_status(goal_id, base)
    _apply_corrections(store, goal_id, ev)
    if ev.verdict == "off_track" and ev.corrections:
        await _notify(notifier, NotifyLevel.TASK, f"🧭 [{goal_id}] course-correcting — {ev.rationale[:200]}")
    return None


async def _resolve_done_gate(
    goal_id: str, goal: Goal, status: GoalStatus, review_report: str,
    *, store: GoalStore, evaluator_caller: ClaudeCaller, notifier: Notifier,
    summarize: "ClaudeCaller | None" = None,
) -> Outcome:
    """A done-gate review just finished — judge the repo against done_when. Only
    'achieved' closes the goal; otherwise corrections are steered back in and the
    goal continues (its next tick plans the next step)."""
    try:
        ev = await _evaluator.evaluate(
            goal, status, store.recent_log(goal_id), store.recent_deliveries(goal_id),
            claude_caller=evaluator_caller, review_report=review_report, at_done_gate=True,
            spec=store.read_spec(goal_id),
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
        await _notify(notifier, NotifyLevel.OWNER, f"✅ [{goal_id}] goal complete (verified) — {ev.rationale[:200]}", summarize=summarize)
        return Outcome.DONE
    if ev.verdict in ("stalled", "needs_human"):
        q = ev.question or ev.rationale or "done-gate flagged a problem"
        store.save_status(goal_id, replace(base, phase="blocked", blocked_on=q, next=""))
        await _notify(notifier, NotifyLevel.OWNER, f"🟡 [{goal_id}] not done — {q}", summarize=summarize)
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
    note: str, summarize: "ClaudeCaller | None" = None,
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
        summarize=summarize,
    )


async def _open_investigation(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep, summarize: "ClaudeCaller | None" = None,
) -> Outcome:
    """A new outcome goal investigates before it executes: dispatch a read-only
    analysis of the repo as it is today. Its terminal result feeds the discovery
    synthesis (``_resolve_discovery``), not the planner. Research, then act — the
    senior-dev move. On any prep/dispatch failure, skip straight to executing
    rather than wedge the goal (investigation is an enhancement, not a gate)."""
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url)
    except WorkspaceError as exc:
        store.append_log(goal_id, f"investigation prep failed ({exc}) — skipping to executing")
        store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle"))
        return Outcome.SLEPT
    review = Action(
        engine="devclaw", tool="review_repository",
        goal=(
            f"Read-only analysis: what does this repository actually do today?\n"
            f"The owner's desired outcome is: {goal.objective}\n"
            f"Describe the current functionality, structure, and how close it is to that outcome."
        ),
        open_pr=False,
    )
    try:
        ref = await engine.dispatch(review, goal, notify_url)
    except Exception as exc:  # noqa: BLE001
        store.append_log(goal_id, f"investigation dispatch failed ({exc}) — skipping to executing")
        store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle"))
        return Outcome.SLEPT
    ref = replace(ref, is_discovery=True)
    store.save_status(goal_id, replace(status, lifecycle="investigating", phase="in_flight", in_flight=ref))
    store.append_log(goal_id, f"investigating → repo analysis {ref.id}")
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🔍 [{goal_id}] taking a look at what's there today — I'll come back with what it does and what 'better' could mean",
        summarize=summarize,
    )
    return Outcome.DISPATCHED


async def _resolve_discovery(
    goal_id: str, goal: Goal, status: GoalStatus, repo_analysis: str,
    *, store: GoalStore, research_caller: ClaudeCaller, notifier: Notifier,
    summarize: "ClaudeCaller | None" = None, grill_caller: "ClaudeCaller | None" = None,
) -> Outcome:
    """The investigating analysis finished — synthesize the discovery brief
    (current state · gap-to-good · best-practice checklist) and persist it. Then,
    if the grill is on, move into grilling (align on scope) primed with the brief;
    otherwise tell the owner plainly and execute. Synthesis failure is non-fatal."""
    try:
        brief = await _research.discovery_brief(goal, repo_analysis, caller=research_caller)
        store.write_discovery(goal_id, brief)
        store.append_log(goal_id, "discovery brief written")
        synth_ok = True
    except Exception as exc:  # noqa: BLE001 — investigation must not wedge the goal
        store.append_log(goal_id, f"discovery synthesis failed ({exc}) — proceeding")
        synth_ok = False

    # Grill on → align on scope (primed with the brief) before executing.
    if _grill.GRILL_ENABLED and grill_caller is not None:
        store.save_status(goal_id, replace(status, lifecycle="grilling", phase="idle"))
        store.append_log(goal_id, "discovery done → grilling")
        return await _run_grill(
            goal_id, goal, store.load_status(goal_id),
            store=store, grill_caller=grill_caller, notifier=notifier, summarize=summarize,
        )

    # Grill off → straight to executing (the brief still informs the planner).
    store.save_status(
        goal_id, replace(status, lifecycle="executing", phase="idle", next="discovery done → executing"),
    )
    msg = (
        f"🔍 [{goal_id}] I looked at what's there for \"{goal.objective}\" — "
        f"I've written up what it does today and what 'better' looks like. Starting work now."
        if synth_ok else f"🔍 [{goal_id}] starting work on \"{goal.objective}\""
    )
    await _notify(notifier, NotifyLevel.OWNER, msg, summarize=summarize)
    return Outcome.SLEPT


async def _run_grill(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, grill_caller: "ClaudeCaller | None", notifier: Notifier,
    summarize: "ClaudeCaller | None" = None,
) -> Outcome:
    """One grill turn over the durable transcript. Quota-safe: if the last question
    is still awaiting the owner's reply, do nothing (zero tokens). Otherwise run the
    elicitation cognition — ask the next question, or finalize the spec and move to
    plan_review. A grill failure degrades to executing rather than wedging."""
    transcript = store.read_grill(goal_id)
    if transcript and "answer" not in transcript[-1]:
        # a question is out; waiting for the owner — zero tokens
        store.save_status(goal_id, replace(status, last_tick_at=store.now_iso()))
        return Outcome.IDLE
    if grill_caller is None:  # defensive — shouldn't reach here with grill on
        store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle"))
        return Outcome.SLEPT
    try:
        step = await _grill.next_step(_grill.grill_idea(goal, store.read_discovery(goal_id)), transcript, grill_caller)
    except Exception as exc:  # noqa: BLE001 — never wedge a goal on a grill hiccup
        store.append_log(goal_id, f"grill failed ({exc}) — proceeding to executing")
        store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle"))
        await _notify(notifier, NotifyLevel.OWNER, f"🟢 [{goal_id}] starting work on \"{goal.objective}\"", summarize=summarize)
        return Outcome.SLEPT

    if step["action"] == "ask":
        transcript.append({"question": step["question"], "recommended": step.get("recommended", "")})
        store.write_grill(goal_id, transcript)
        store.append_log(goal_id, f"grill Q{len(transcript)}: {step['question']}")
        rec = f"\n(suggested: {step['recommended']})" if step.get("recommended") else ""
        await _notify(
            notifier, NotifyLevel.OWNER,
            f"❓ [{goal_id}] {step['question']}{rec}", summarize=summarize,
        )
        return Outcome.ASKED

    # action == "done" → spec agreed; present the plan for approval.
    store.write_spec(goal_id, step["spec"])
    store.save_status(goal_id, replace(status, lifecycle="plan_review", phase="idle", next="awaiting plan approval"))
    store.append_log(goal_id, "grill done → spec written → plan_review")
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"📋 [{goal_id}] Here's the plan for \"{goal.objective}\":\n\n{step['spec']}\n\nReply to approve and I'll start.",
        summarize=summarize,
    )
    return Outcome.ASKED


async def _dispatch_action(
    goal_id: str, goal: Goal, base: GoalStatus, action: Action,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep,
    summarize: "ClaudeCaller | None" = None,
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
        await _notify(notifier, NotifyLevel.OWNER, f"🛑 [{goal_id}] dispatch cap ({cap}) reached — paused for your review", summarize=summarize)
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
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    grill_caller: "ClaudeCaller | None" = None,
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
                summary_caller=summary_caller, merger=merger, grill_caller=grill_caller,
            )
        except Exception:  # noqa: BLE001 — isolate per-goal blast radius
            store.append_log(goal_id, "tick crashed (uncaught)")
            outcomes[goal_id] = Outcome.ERROR
    return outcomes
