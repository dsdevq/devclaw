"""Blocking guards + the no-progress watchdog — the goal-tick failure handlers.

These are the "fail loud, not silent" handlers (CLAUDE.md hardening philosophy):
a workspace-prep failure, a corrupt contract file, a lost in-flight ref each
block the goal legibly with an owner ping instead of wedging the tick loop; the
watchdog fires one owner ping when an executing goal stops shipping. Split out of
:mod:`devclaw.goal.tick`; imported by tick_dispatch / tick_settle and re-exported
from tick.py.
"""

from __future__ import annotations

from dataclasses import replace

from .tick_context import NotifyLevel, Outcome, TickContext, _notify
from .engine import GoalEngineError
from .models import Goal, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalDocCorrupt, GoalStore
from .transitions import Event
from ..engine.workspace import WorkspaceError


def _progress_window_active(status: GoalStatus) -> bool:
    """Is this a goal the no-progress watchdog should measure? Only one that is
    actively executing — not waiting on the owner (the `blocked` phase, which
    already pinged), not done/cancelled (returned earlier). 'verifying' counts
    (the done-gate review is still work in flight)."""
    return (status.lifecycle or "executing") == "executing" and status.phase in (
        "idle", "in_flight", "verifying",
    )


async def _check_no_progress(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, notifier: Notifier, window_s: int,
    summarize: "ClaudeCaller | None" = None,
) -> GoalStatus:
    """Zero-token wall-clock watchdog. Self-initializes the progress baseline the
    first time it sees an executing goal, then fires exactly one OWNER ping if the
    goal has gone ``window_s`` without a delivery. Returns the (possibly updated)
    status so the caller carries the baseline/flag forward instead of clobbering it.

    Pure mechanism: it reads timestamps and never calls cognition on the measuring
    path (the summarizer only runs for the one ping that actually clears the gate)."""
    if window_s <= 0 or not _progress_window_active(status):
        return status
    if status.last_progress_at is None:
        # start the clock — the goal just began executing (covers legacy goals and
        # any path into executing, without touching every transition site).
        # Telemetry-only field → update_status_fields, never a full-row rewrite
        # (a full save_status here would be the exact stale-snapshot clobber
        # class this PR closes: a watchdog init racing a concurrent
        # phase-changing write must never win).
        status = store.update_status_fields(goal_id, last_progress_at=store.now_iso())
        return status
    elapsed = store.seconds_since(status.last_progress_at)
    if elapsed is None or elapsed < window_s or status.no_progress_notified:
        return status
    hours = round(elapsed / 3600, 1)
    status = store.update_status_fields(goal_id, no_progress_notified=True)
    store.append_log(goal_id, f"no-progress watchdog fired — ~{hours}h since last delivery")
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🐢 [{goal_id}] no progress in ~{hours}h on \"{goal.objective}\" — "
        f"it's still working but nothing has shipped; you may want to take a look",
        summarize=summarize,
    )
    return status


async def _block_on_prep_failure(
    goal_id: str, status: GoalStatus, exc: "WorkspaceError",
    *, store: GoalStore, notifier: Notifier, summarize: "ClaudeCaller | None",
) -> Outcome:
    """A workspace couldn't be prepared — a bad/missing ``repo_url``, a clone
    that 404s (the repo doesn't exist *or* is private and unreadable — GitHub
    returns the same "not found" for both), an auth or fetch failure. None of
    these self-heal on the next tick, so the old behaviour (log it, drop to
    ``phase=idle``, notify at TASK altitude) made the goal look merely idle while
    it silently re-tried the same doomed clone every cadence — invisible to the
    owner, who then can't tell a wedged goal from one with nothing to do.

    Instead: block with the *real* git error as ``blocked_on`` and tell the owner
    at OWNER altitude. ``lifecycle`` is pinned to ``executing`` so subsequent
    ticks route through the blocked-guard in :func:`tick_goal` (cadence does not
    re-poke a blocked goal — only steering does), making this a single, legible
    notification rather than a per-tick loop. When the owner answers/steers (e.g.
    fixes the repo_url), the goal unblocks and prep is retried with the fix."""
    msg = str(exc)
    store.append_log(goal_id, f"workspace prep failed — blocking for the owner: {msg}")
    store.transition(
        goal_id, Event.BLOCK,
        replace(status, lifecycle="executing", phase="blocked", blocked_on=msg,
                blocked_kind="mechanical:prep", in_flight=None, next=""),
        expect=status,
    )
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🟡 [{goal_id}] I couldn't set up the workspace, so I've paused — {msg}",
        summarize=summarize,
    )
    return Outcome.BLOCKED


async def _block_on_corrupt_doc(
    goal_id: str, status: GoalStatus, exc: "GoalDocCorrupt",
    *, store: GoalStore, notifier: Notifier, summarize: "ClaudeCaller | None",
) -> Outcome:
    """A goal contract file (checklist.yaml / firmed-draft.yaml) EXISTS on disk
    but won't parse. Before T0.4 this degraded SILENTLY: a torn checklist read
    as "no checklist" and flipped the goal into the backlog planning pipeline;
    a torn firmed draft made ``load_effective_goal`` return the base goal,
    dropping the firmed done_when / stub_acceptable / verify_cmd contract with
    zero signal. Neither self-heals — nothing rewrites these files on its own.

    So: block with the real parse error as ``blocked_on`` and tell the owner
    once, at OWNER altitude (same shape as :func:`_block_on_prep_failure`).
    ``in_flight`` is preserved AS-IS — blocking stops new cognition, it must
    not orphan a running action; the ref settles normally once the file is
    fixed. ``lifecycle`` is pinned to ``executing`` so the goal routes through
    the blocked-guard once it can tick again. A repeat tick on the SAME
    corruption idles quietly (no log spam, no re-ping) — this handler runs
    before the blocked-guard can gate it, so it dedupes on ``blocked_on``
    itself. Recovery: fix (or delete) the file, then steer."""
    msg = str(exc)
    if status.phase == "blocked" and status.blocked_on == msg:
        store.update_status_fields(goal_id, last_tick_at=store.now_iso())
        return Outcome.IDLE
    store.append_log(goal_id, f"goal contract file corrupt — blocking for the owner: {msg}")
    store.transition(
        goal_id, Event.BLOCK,
        replace(status, lifecycle="executing", phase="blocked", blocked_on=msg,
                blocked_kind="mechanical:corrupt_doc", next=""),
        expect=status,
    )
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🟡 [{goal_id}] a goal contract file is corrupted — I've paused rather than "
        f"work from the wrong contract; fix or steer: {msg}",
        summarize=summarize,
    )
    return Outcome.BLOCKED


async def _block_on_lost_ref(
    goal_id: str, status: GoalStatus, exc: GoalEngineError, ctx: TickContext,
) -> Outcome:
    """The in-flight ref points at a task/program row the engine no longer has
    (a lost/replaced SQLite DB, manual row cleanup, a cross-environment
    restore). The row never comes back, so an unguarded poll raises into
    tick_all's per-goal catch-all — which logs "tick error (isolated)" but
    never clears ``in_flight``, and the goal re-raises identically on EVERY
    subsequent tick: a silent, permanent error loop the owner never hears
    about (audit-found 2026-07-10).

    Instead: clear the lost ref, block with the real error as ``blocked_on``,
    and tell the owner ONCE at OWNER altitude. Blocked goals are not re-poked
    by cadence (see :func:`_handle_executing`) — only steering unblocks them —
    so this is one legible failure, and the owner decides how to proceed
    (typically steer_goal to re-plan). ``lifecycle`` is pinned to ``executing``
    for the same reason :func:`_block_on_prep_failure` pins it: a lost
    DISCOVERY ref would otherwise leave ``lifecycle="investigating"``, which
    :func:`_classify` routes straight back into INVESTIGATING on the next tick
    — a fresh dispatch that silently contradicts the "paused; steer me" ping
    the owner just received. Catches :class:`GoalEngineError` ONLY — a real
    bug must still surface through the catch-all, not be absorbed as a lost
    ref."""
    ref = status.in_flight
    msg = f"lost in-flight {ref.ref_kind} {ref.id} — {exc}"
    ctx.store.append_log(goal_id, f"poll failed — blocking for the owner: {msg}")
    ctx.store.transition(
        goal_id, Event.BLOCK,
        replace(
            status, lifecycle="executing", in_flight=None,
            phase="blocked", blocked_on=msg, blocked_kind="mechanical:lost_ref", next="",
        ),
        expect=status,
    )
    await _notify(
        ctx.notifier, NotifyLevel.OWNER,
        f"🟡 [{goal_id}] I lost track of the in-flight work ({ref.ref_kind} {ref.id}) — "
        "paused; steer me to continue",
        summarize=ctx.summary_caller,
    )
    return Outcome.BLOCKED
