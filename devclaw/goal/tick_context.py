"""Shared tick primitives — the leaf of the goal-heartbeat module split.

Constants, the :class:`Outcome` / :class:`NotifyLevel` / :class:`Phase` enums,
:class:`TickContext`, the phase classifier, the per-goal tick lock, the notify
helpers, and the atomic-dispatch primitives. Everything here is imported by the
other ``tick_*`` modules and re-exported from :mod:`devclaw.goal.tick` so the
public surface (and every test import) is unchanged.

Preserving :class:`Outcome`'s identity HERE is load-bearing — ``service.py`` and
every test compare against this one object; the ``isinstance(outcome, Outcome)``
chain in ``_tick_goal_impl`` relies on it being a single class.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from . import merge as _merge
from . import remote_checks as _remote_checks
from . import summary as _goal_summary
from .engine import GoalEngine
from .models import EvalResult, GoalStatus
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalStore
from ..engine.workspace import prepare_workspace
from ..loom import trace as _trace


#: (workspace_dir, repo_url, branch) -> the branch name actually checked out.
#: ``branch=None`` keeps the legacy behaviour (default branch); a goal-scoped
#: ``"goal/<id>"`` branch is passed when checklist mode wants every item to
#: stack on the same branch instead of forking off main. Injected so tests
#: pass a no-op.
WorkspacePrep = Callable[[str, "str | None", "str | None"], Awaitable[str]]


#: deliveries between periodic direction evaluations (0 → only at the done-gate)
EVAL_EVERY = int(os.environ.get("DEVCLAW_GOAL_EVAL_EVERY", "3"))


#: wall-clock seconds an EXECUTING goal may go without a delivery before the
#: no-progress watchdog pings the owner once. Complements the per-task timeout
#: (which kills one hung run) by catching a goal that keeps churning — dispatching,
#: failing the gate, re-planning — without ever shipping. 0 disables. Default 6h.
NO_PROGRESS_S = int(os.environ.get("DEVCLAW_GOAL_NO_PROGRESS_S", "21600"))


#: when True, a planner "done" proposal dispatches a read-only review of the repo
#: against done_when and the evaluator judges THAT before the goal closes.
VERIFY_DONE = True


#: when True, a goal reaching `achieved` auto-deploys the built app to a durable
#: Tailscale URL. The devclaw-wide default; a project may override it.
AUTODEPLOY_ENABLED = True


#: when True, the investigating phase dispatches the decomposer after the
#: discovery brief is written — emitting an atomic checklist that the per-tick
#: planner picks actions from instead of the free-form backlog. Pillar 1 of the
#: planning-engine rework; default OFF so legacy goals are unaffected until the
#: operator opts in (per-goal env or stack-wide).
DECOMPOSE_ENABLED = os.environ.get("DEVCLAW_GOAL_DECOMPOSE", "0") not in ("0", "false", "")


class Outcome(str, Enum):
    IDLE = "idle"            # cheap check found nothing — 0 tokens
    IN_FLIGHT = "in_flight"  # dispatched action still running — 0 tokens
    DISPATCHED = "dispatched"
    VERIFYING = "verifying"  # done-gate review dispatched
    SLEPT = "slept"
    ADVANCED = "advanced"    # lifecycle transitioned without dispatching a task; re-tick immediately
    BLOCKED = "blocked"
    DONE = "done"
    SKIP_DONE = "skip_done"
    SKIP_CANCELLED = "skip_cancelled"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"  # paused on a usage/quota limit — 0 tokens, auto-resumes
    CONFLICT = "conflict"  # steer/cancel landed mid-tick; the tick's write was abandoned; next tick reads fresh


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


def _action_label(ref) -> str:
    """A SHORT human label for an action — its first line, trimmed. An action's
    full ``goal`` is a long instruction prompt; putting it in a notification (which
    happens when the plain-language summarizer is quota-blocked and falls back to
    the raw text) spams the owner with the entire prompt. Keep the notification
    terse BY CONSTRUCTION so it reads well even without the summarizer."""
    text = (getattr(ref, "goal", None) or getattr(ref, "tool", "") or "change").strip()
    first = text.splitlines()[0].strip() if text else "change"
    return (first[:90].rstrip() + "…") if len(first) > 90 else first


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
    _trace.record_notify(level=level.name, text=text)
    await notifier.send(text)


#: The self-triage allowlist (slice 1). Only owner pings whose ``kind`` is in
#: this set route through the propose-only triage interceptor; every other ping
#: stays on the raw path, byte-identical. Trigger granularity is deliberately an
#: ALLOWLIST, not "every owner ping" — the blast radius stays tiny and each new
#: trigger is an explicit, reviewed addition. Slice 1 registers exactly one key;
#: a future ``needs_answer`` wire adds "needs_answer" here and calls
#: :func:`triaged_notify` from the block path.
TRIAGE_ELIGIBLE = {"db_size"}


async def triaged_notify(
    notifier: Notifier, level: NotifyLevel, raw_text: str,
    *, kind: str, triage_caller: "ClaudeCaller | None",
    catalog: str = "", repo_context: str = "",
    summarize: "ClaudeCaller | None" = None,
) -> None:
    """The propose-only interception choke point (self-triage slice 1).

    Before an OWNER ping goes out, if its ``kind`` is on the :data:`TRIAGE_ELIGIBLE`
    allowlist AND a ``triage_caller`` is wired, route it through the bounded
    triage cognition step: dedupe against the ``problems`` catalog + draft a
    proposed fix, then deliver "problem + proposed fix + how to approve" instead
    of the bare alert. Otherwise (no caller, or an ineligible kind) this is
    byte-identical to a plain :func:`_notify`.

    Fails toward the owner: triage never raises, and if it returns no proposal
    (LLM error, invalid JSON, empty fix) the ORIGINAL raw ping is delivered
    unchanged — loud, not silent. Zero-token idle guard intact: this only runs
    when the caller already decided a real ping should fire (never on idle).

    ``summarize`` is applied ONLY to the raw fallback path — an enriched proposal
    is already plain owner-facing prose and must not be re-summarized (that would
    risk dropping the proposed fix / approve line)."""
    if triage_caller is None or kind not in TRIAGE_ELIGIBLE:
        await _notify(notifier, level, raw_text, summarize=summarize)
        return
    try:
        from . import triage as _triage  # lazy — avoids any import cycle
        proposal = await _triage.triage(
            raw_text, catalog=catalog, repo_context=repo_context, caller=triage_caller,
        )
    except Exception:  # noqa: BLE001 — interception must never break the heartbeat
        proposal = None
    if proposal is None:
        await _notify(notifier, level, raw_text, summarize=summarize)
        return
    from . import triage as _triage  # (cached import) render the enriched message
    await _notify(notifier, level, _triage.render(proposal, raw_text))


class Phase(str, Enum):
    """The reified state of a goal at the start of a tick — derived from
    ``status`` by :func:`_classify`. Drives the handler dispatch in
    :func:`tick_goal`.

    Polling phases mean "an action is in flight; settle it." Lifecycle phases
    mean "no in-flight work; do this lifecycle step." Terminal phases are
    fast-paths that skip even the watchdog."""

    TERMINAL_DONE = "terminal_done"
    TERMINAL_CANCELLED = "terminal_cancelled"
    POLLING_DISCOVERY = "polling_discovery"
    POLLING_DONE_GATE = "polling_done_gate"
    POLLING_ACTION = "polling_action"
    INVESTIGATING = "investigating"
    FIRMING = "firming"
    EXECUTING = "executing"


def _classify(status: GoalStatus) -> Phase:
    """Map a goal's status to its current phase. Single source of truth for the
    state-machine — every transition is implicitly through what gets saved to
    ``status`` (lifecycle / in_flight / phase fields), and reading them here
    keeps the dispatch one switch instead of a waterfall of branches."""
    if status.phase == "done":
        return Phase.TERMINAL_DONE
    if status.phase == "cancelled":
        return Phase.TERMINAL_CANCELLED
    if status.in_flight is not None:
        ref = status.in_flight
        if getattr(ref, "is_discovery", False):
            return Phase.POLLING_DISCOVERY
        if getattr(ref, "is_done_check", False):
            return Phase.POLLING_DONE_GATE
        return Phase.POLLING_ACTION
    # No in-flight work — dispatch on lifecycle. A None lifecycle (legacy goal)
    # behaves as "executing"; "investigating" without an in-flight ref re-opens
    # the investigation (the discovery never resolved — dispatch it again).
    lifecycle = status.lifecycle or "executing"
    if lifecycle == "investigating":
        return Phase.INVESTIGATING
    if lifecycle == "firming":
        return Phase.FIRMING
    return Phase.EXECUTING


@dataclass(frozen=True)
class TickContext:
    """All the deps a single tick needs, bundled so handlers take one parameter
    instead of twelve. Read-only; if a handler mutates state it does so through
    ``ctx.store``."""

    store: GoalStore
    engine: GoalEngine
    planner_caller: ClaudeCaller
    evaluator_caller: ClaudeCaller
    notifier: Notifier
    notify_url: str = ""
    prepare_ws: WorkspacePrep = prepare_workspace
    eval_every: int = EVAL_EVERY
    verify_done: bool = VERIFY_DONE
    autodeploy: bool = AUTODEPLOY_ENABLED
    no_progress_s: int = NO_PROGRESS_S
    decompose_enabled: bool = DECOMPOSE_ENABLED
    summary_caller: "ClaudeCaller | None" = None
    merger: "_merge.Merger | None" = None
    #: Pillar 1 cognition caller for the decomposer. None → reuse
    #: ``planner_caller`` (both default to opus); explicit injection lets tests
    #: stub the decomposer without touching the planner stub.
    decomposer_caller: "ClaudeCaller | None" = None
    #: cognition caller for world-research (from-scratch goal grounding —
    #: real-world exemplars + MVP bar + defer list). None → handlers use
    #: ``world_research.default_caller()``; explicit injection lets tests stub
    #: the brief without touching subprocess.
    world_research_caller: "ClaudeCaller | None" = None
    #: grounded remote-checks verification at the done-gate (the 2026-07-06
    #: benchmark fix: ``achieved`` is only honored when the goal branch's REAL
    #: CI doesn't contradict it). None → skipped (legacy behaviour);
    #: goal_service binds the gh-backed checker, tests inject a fake — the
    #: same subprocess-free-tick seam as ``merger``.
    remote_checker: "_remote_checks.RemoteChecker | None" = None


# ---- per-goal tick serialization (Tranche 1/PR8) ---------------------------
#
# CAS (GoalStore.transition's optimistic-concurrency check) already guarantees
# CORRECTNESS when two ticks race the SAME goal — an MCP-driven tick_one
# (manual poke, ops-agent) overlapping the heartbeat's tick_all is the one
# remaining same-goal concurrency left after PR4. Pre-PR8, that race meant
# BOTH ticks ran a full cognition round (a planner/evaluator call can take
# minutes) and the loser abandoned its ENTIRE planning round to a
# TransitionConflict — correct, but a wasted round of tokens and a confusing
# trace. This lock adds EFFICIENCY + LEGIBILITY on top of CAS's correctness:
# the second tick simply waits for the first to finish, then reads FRESH
# state — usually landing on IDLE at zero cognition cost instead of losing a
# race it already lost the moment it started.
#
# Deliberately scoped to tick_goal ONLY — steer_goal / cancel_goal /
# evaluate_goal stay lock-free on purpose. They are synchronous, loop-atomic
# MCP calls that must never wait behind a minutes-long cognition await; CAS
# remains their guard, unchanged. This is the design's decision, verbatim:
# "one per-goal asyncio.Lock around tick_goal only; steer/cancel stay sync +
# loop-atomic."
#
# Unbounded growth (a Lock object per goal id, forever, even for done/
# cancelled goals) is fine — goals number in the dozens, not millions; the
# per-goal Lock is a few dozen bytes and there is no eviction path worth the
# complexity for a fleet this small.
_TICK_LOCKS: dict[str, asyncio.Lock] = {}


def _tick_lock(goal_id: str) -> asyncio.Lock:
    return _TICK_LOCKS.setdefault(goal_id, asyncio.Lock())


def _run_atomic(coro):
    """Drive ``coro`` SYNCHRONOUSLY to completion, enforcing that it never
    actually suspends. ``engine.dispatch()`` runs INSIDE an open
    ``store.transaction()`` (see the dispatch sites below) — the transaction
    holds the shared StateStore's lock for its whole extent, so anything that
    genuinely awaits an I/O boundary in there would hold that lock for as
    long as the dispatch takes, and — worse — could not be rolled back
    cleanly if it partially ran before a later failure. ``InProcessEngine.
    dispatch`` and ``tests.goal_fakes.FakeEngine.dispatch`` are both
    fully synchronous under the hood (task/program creation is a plain
    SQLite write; with PR7's ``pump=False`` split, dispatch no longer spawns
    background execution either) — zero real awaits — so driving them with a
    single ``.send(None)`` always completes in one step.

    A coroutine that DOES suspend (a hypothetical remote/HTTP engine) raises
    RuntimeError here instead of silently holding the transaction open across
    a real suspend point — such an engine cannot be atomic and must not
    pretend to be; this is the mechanical enforcement of that invariant."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    else:
        coro.close()
        raise RuntimeError(
            "engine.dispatch yielded inside the atomic dispatch transaction "
            "— only a fully-synchronous dispatch (no real awaits) can join "
            "the transactional unit; a remote/HTTP engine cannot be atomic "
            "and must not be driven this way"
        )


def _engine_kick(engine: GoalEngine) -> None:
    """Nudge the task queue to claim + launch the just-committed pending
    row, if the engine exposes a ``kick()`` (the in-process engine does;
    test doubles may not → silently no-op, same getattr/callable pattern as
    ``_engine_pause`` and friends below). Called AFTER a dispatch
    transaction commits — crash between commit and kick is self-healing:
    the row is durably 'pending' either way, and the queue's own
    ``start_ticking`` loop pumps every ``TICK_SECONDS`` regardless."""
    fn = getattr(engine, "kick", None)
    if callable(fn):
        fn()


def _apply_corrections(store: GoalStore, goal_id: str, ev: EvalResult) -> None:
    if ev.corrections:
        store.append_steering(goal_id, ev.corrections, source="auto-eval")
