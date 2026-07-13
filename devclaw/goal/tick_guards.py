"""Blocking guards + the no-progress watchdog — the goal-tick failure handlers.

These are the "fail loud, not silent" handlers (CLAUDE.md hardening philosophy):
a workspace-prep failure, a corrupt contract file, a lost in-flight ref each
block the goal legibly with an owner ping instead of wedging the tick loop; the
watchdog fires one owner ping when an executing goal stops shipping. Split out of
:mod:`devclaw.goal.tick`; imported by tick_dispatch / tick_settle and re-exported
from tick.py.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta

from .tick_context import NotifyLevel, Outcome, TickContext, _notify
from .engine import GoalEngineError
from .models import Goal, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalDocCorrupt, GoalStore
from .transitions import Event
from ..engine.workspace import WorkspaceError
from ..task_git import _ls_remote_ok_sync


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
    itself. Recovery: fix (or delete) the file — the next tick's contract
    probe auto-heals the block mechanically (:func:`_autoheal_corrupt_doc`,
    damped by ``heal_attempts``) — or steer."""
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
    ref.

    DELIBERATELY HUMAN-GATED — never auto-healed. Unlike a corrupt doc (the
    file can parse again) or a prep failure (the remote can come back), this
    block is structurally unhealable by mechanism: ``in_flight`` is destroyed
    right here at block time (the ``in_flight=None`` below), so the lost id
    survives only in the ``blocked_on`` prose — there is nothing machine-
    readable left for a recheck to re-poll, and re-attaching from prose would
    be exactly the string-matching ``blocked_kind`` exists to forbid. The
    owner decides how to proceed (typically steer_goal to re-plan)."""
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


#: Max mechanical auto-heals for one goal before the loop hands the block back
#: to a human. Damping is MANDATORY, not a nicety: the quota pause's auto-resume
#: (tick_all) needed none because its heal signal is monotone (time only moves
#: forward), but a mechanical condition can FLAP — block → heal → re-block —
#: and an undamped heal would convert the zero-token blocked steady-state into
#: a planner call (+ a block ping) per cycle. Past the cap the goal stays
#: blocked at zero cost until a human lifts it (steer_goal), which restores the
#: budget; a productive settle also earns it back (see tick_settle).
CORRUPT_DOC_HEAL_CAP = 3


async def _autoheal_corrupt_doc(
    goal_id: str, status: GoalStatus,
    *, store: GoalStore, notifier: Notifier,
) -> "GoalStatus | None":
    """Mechanically lift a ``mechanical:corrupt_doc`` block whose condition no
    longer holds. The caller (the tick's contract-file choke point) has JUST
    re-parsed the contract docs successfully — that probe runs every tick
    anyway, so the recheck is free: zero LLM, zero subprocess, the exact
    mirror of the quota pause's timestamp-compare auto-resume.

    Fires ONLY on ``blocked_kind == "mechanical:corrupt_doc"`` (the caller
    gates on it). Never on ``needs_answer`` (the owner must answer), ``bug``
    (the force_block escape hatch), ``mechanical:lost_ref`` (structurally
    unhealable — see :func:`_block_on_lost_ref`), or ``mechanical:dispatch_cap``
    (a review-my-PRs backstop, a human decision by design).

    Healing means RE-ATTEMPTING, not suppressing: the write mirrors
    resume_goal's shape (actions + plan cadence reset so the tick actually
    re-plans — the ensuing plan is the intended cost of a real heal), and a
    preserved in-flight ref is restored to its polling phase instead of
    orphaned. The block itself stays exactly as loud as today; the only
    notification this path ever sends is the gave-up ping when the
    ``heal_attempts`` budget (see :data:`CORRUPT_DOC_HEAL_CAP`) runs out —
    sent PLAIN (never through the summarizer LLM), exactly once (the counter
    is bumped one past the cap as the pause_notified-style once-marker).

    Returns the healed (fresh-versioned) status, or ``None`` when it refused
    to heal — the caller then proceeds with the still-blocked status, which
    idles at zero cognition like any other blocked tick."""
    if status.heal_attempts > CORRUPT_DOC_HEAL_CAP:
        # Budget exhausted AND the owner already heard the gave-up ping (the
        # sentinel bump below) — stay blocked, zero cost, until a human lifts it.
        return None
    if status.heal_attempts >= CORRUPT_DOC_HEAL_CAP:
        await _heal_give_up(
            goal_id, store=store, notifier=notifier, cap=CORRUPT_DOC_HEAL_CAP,
            reason="the contract file keeps re-corrupting after each fix",
        )
        return None
    n = status.heal_attempts + 1
    healed = _heal_unblock(goal_id, status, store, heal_attempts=n)
    store.append_log(
        goal_id,
        f"auto-resumed: contract file parses again (heal {n}/{CORRUPT_DOC_HEAL_CAP})",
    )
    return healed


#: Prep-heal budget. Larger than the corrupt-doc cap on purpose: a transient
#: remote outage (GitHub incident, DNS blip) legitimately takes several
#: backoff windows to clear, while a contract file that keeps re-corrupting
#: after 3 fixes is somebody actively fighting the store.
PREP_HEAL_CAP = 5

#: Exponential backoff for the prep recheck: 30min · 2^heal_attempts, capped.
#: The corrupt-doc recheck is FREE (the tick's contract probe runs anyway);
#: this one is a git subprocess, so between windows a blocked goal must stay
#: a zero-subprocess tick — the persisted ``next_heal_at`` window enforces it.
PREP_BACKOFF_BASE_S = 30 * 60
PREP_BACKOFF_MAX_S = 6 * 3600


async def _prep_recheck_ok(goal: Goal) -> bool:
    """The mechanical prep recheck — no LLM, best-effort, never raises.

    With a ``repo_url``: one ``git ls-remote`` (offloaded to a thread — it can
    block up to its 10s timeout) probing the exact surface prepare_workspace's
    clone/fetch fails on. Without one (pre-existing-workspace config, where
    prep only resets the checkout): does ``<workspace_dir>/.git`` exist —
    a stat, no subprocess."""
    if not goal.repo_url:
        return os.path.isdir(os.path.join(goal.workspace_dir, ".git"))
    return await asyncio.to_thread(_ls_remote_ok_sync, goal.repo_url)


async def _autoheal_prep(
    goal_id: str, goal: Goal, status: GoalStatus,
    *, store: GoalStore, notifier: Notifier,
) -> "GoalStatus | None":
    """Mechanically lift a ``mechanical:prep`` block once the repo is reachable
    again — the sibling of :func:`_autoheal_corrupt_doc` (same scope rules,
    same damping contract, same plain-ping-only-on-give-up notification
    policy; see its docstring), with one difference: the recheck COSTS a git
    subprocess, so it runs on a persisted exponential backoff instead of every
    tick. ``next_heal_at`` gates it — before that instant the tick returns
    immediately (zero subprocess, zero cognition); a FAILED recheck pushes the
    window out (30min · 2^attempts, capped at 6h) and spends one attempt.

    A successful recheck fires the same resume-shaped UNBLOCK as the
    corrupt-doc heal: the next dispatch runs the REAL prepare_ws — ls-remote
    proves reachability, not that the clone will succeed — and if prep still
    fails it re-blocks loudly and the backoff continues where it left off
    (``heal_attempts`` persists across the heal; only a human unblock or a
    productive settle resets it).

    Returns the healed status, or ``None`` (parked / window not open /
    still unreachable)."""
    if status.heal_attempts > PREP_HEAL_CAP:
        return None  # parked — the gave-up ping already went out
    if status.heal_attempts >= PREP_HEAL_CAP:
        await _heal_give_up(
            goal_id, store=store, notifier=notifier, cap=PREP_HEAL_CAP,
            reason="the workspace still can't be prepared",
        )
        return None
    # Backoff window: no recheck — not even the subprocess — before it opens.
    remaining = store.seconds_since(status.next_heal_at)
    if status.next_heal_at and remaining is not None and remaining < 0:
        return None
    if not await _prep_recheck_ok(goal):
        backoff_s = min(PREP_BACKOFF_MAX_S, PREP_BACKOFF_BASE_S * (2 ** status.heal_attempts))
        next_at = (
            datetime.fromisoformat(store.now_iso()) + timedelta(seconds=backoff_s)
        ).isoformat(timespec="seconds")
        n = status.heal_attempts + 1
        store.update_status_fields(goal_id, heal_attempts=n, next_heal_at=next_at)
        store.append_log(
            goal_id,
            f"prep recheck: repo still unreachable (attempt {n}/{PREP_HEAL_CAP}) — "
            f"next recheck at {next_at}",
        )
        return None
    n = status.heal_attempts + 1
    healed = _heal_unblock(goal_id, status, store, heal_attempts=n)
    store.append_log(
        goal_id,
        f"auto-resumed: repo reachable again (heal {n}/{PREP_HEAL_CAP}) — "
        "next dispatch retries the real workspace prep",
    )
    return healed


def _heal_unblock(
    goal_id: str, status: GoalStatus, store: GoalStore, *, heal_attempts: int,
) -> GoalStatus:
    """The shared resume-shaped UNBLOCK write both mechanical heals fire:
    actions + plan cadence reset so the tick actually re-plans, the backoff
    window cleared, and a preserved in-flight ref (corrupt-doc blocks keep it
    — see :func:`_block_on_corrupt_doc`) restored to its polling phase so it
    settles normally instead of being orphaned."""
    if status.in_flight is not None:
        restored_phase = "verifying" if status.in_flight.is_done_check else "in_flight"
    else:
        restored_phase = "idle"
    return store.transition(
        goal_id, Event.UNBLOCK,
        replace(
            status, phase=restored_phase, blocked_on="",
            actions_dispatched=0, last_plan_at=None,
            heal_attempts=heal_attempts, next_heal_at=None,
        ),
        expect=status,
    )


async def _heal_give_up(
    goal_id: str, *, store: GoalStore, notifier: Notifier, cap: int, reason: str,
) -> None:
    """Park a mechanical block whose heal budget is spent: mark FIRST (the
    sentinel bump one past the cap — a column-only write, the goal stays
    blocked, so this must not be a phase transition; it is what keeps the
    ping to exactly one, the pause_notified pattern), then log, then ONE
    plain owner ping — never through the summarizer LLM."""
    store.update_status_fields(goal_id, heal_attempts=cap + 1)
    store.append_log(
        goal_id, f"auto-recovery gave up after {cap} attempts — {reason}; needs you",
    )
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🟡 [{goal_id}] auto-recovery gave up after {cap} attempts — "
        f"{reason}; needs you (steer to resume)",
    )
