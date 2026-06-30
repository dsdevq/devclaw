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
from dataclasses import dataclass, replace
from enum import Enum
from typing import Awaitable, Callable, Tuple, Union

from . import checklist as _checklist
from . import decomposer as _decomposer
from . import evaluator as _evaluator
from ..delivery import deploy as _deploy
from . import merge as _merge
from . import planner as _planner
from . import research as _research
from . import world_research as _world_research
from . import summary as _goal_summary
from .engine import GoalEngine
from .models import Action, Checklist, EvalResult, Goal, GoalStatus, PollResult
from .notify import Notifier
from .planner import ClaudeCaller
from .store import GoalStore
from ..loom import trace as _trace
from ..loom.limits import classify_failure, pause_seconds
from ..planner import PlannerError
from ..state_store import _now_ms
from ..engine.workspace import WorkspaceError, prepare_workspace

#: (workspace_dir, repo_url, branch) -> the branch name actually checked out.
#: ``branch=None`` keeps the legacy behaviour (default branch); a goal-scoped
#: ``"goal/<id>"`` branch is passed when checklist mode wants every item to
#: stack on the same branch instead of forking off main. Injected so tests
#: pass a no-op.
WorkspacePrep = Callable[[str, "str | None", "str | None", "list[str] | None"], Awaitable[str]]

#: deliveries between periodic direction evaluations (0 → only at the done-gate)
EVAL_EVERY = int(os.environ.get("DEVCLAW_GOAL_EVAL_EVERY", "3"))
#: wall-clock seconds an EXECUTING goal may go without a delivery before the
#: no-progress watchdog pings the owner once. Complements the per-task timeout
#: (which kills one hung run) by catching a goal that keeps churning — dispatching,
#: failing the gate, re-planning — without ever shipping. 0 disables. Default 6h.
NO_PROGRESS_S = int(os.environ.get("DEVCLAW_GOAL_NO_PROGRESS_S", "21600"))
#: when True, a planner "done" proposal dispatches a read-only review of the repo
#: against done_when and the evaluator judges THAT before the goal closes.
VERIFY_DONE = os.environ.get("DEVCLAW_GOAL_VERIFY_DONE", "1") not in ("0", "false", "")
#: when True, the investigating phase dispatches the decomposer after the
#: discovery brief is written — emitting an atomic checklist that the per-tick
#: planner picks actions from instead of the free-form backlog. Pillar 1 of the
#: planning-engine rework; default OFF so legacy goals are unaffected until the
#: operator opts in (per-goal env or stack-wide).
DECOMPOSE_ENABLED = os.environ.get("DEVCLAW_GOAL_DECOMPOSE", "0") not in ("0", "false", "")


def _done_gate_review_brief(goal: "Goal") -> str:
    """The instruction the in-sandbox read-only reviewer gets when the planner
    proposes done. The reviewer's report is then fed to the direction evaluator
    — both sides speak the same vocabulary: ``done_when`` is decomposed into
    atomic clauses, and each clause needs SPECIFIC repo evidence (file path +
    symbol + test name) to count as satisfied. This closes the
    `finance-sentry-mcp-readonly` failure mode (2026-06-25), where the reviewer
    produced a vague prose report, the evaluator stamped it `achieved`, and the
    delivered PR turned out to be 16 stub tools with zero real backend reads.

    Two axes — both load-bearing for the verdict:
      1. PER-CLAUSE EVIDENCE — does the code DO what the goal asked?
      2. STRUCTURAL HEALTH   — is the code Denys would HAND TO A NEW HIRE? (added
                               2026-06-29 after closeloop's App.tsx grew to 1827
                               LOC through 4 PRs that each satisfied the clauses
                               but left the codebase worse.)

    A goal is only ACHIEVED when BOTH axes pass. A green Per-clause section with
    a poor Structural section is OFF_TRACK — the agent must come back and clean
    up before the goal closes."""
    return (
        "Read-only review of this repository to verify whether the goal is "
        "fully satisfied. You produce the GROUNDED evidence the direction "
        "evaluator will judge against — be specific and honest, not generous. "
        "Two axes matter: did the code DO what was asked, AND is the codebase "
        "in a shape a senior engineer would hand to a new hire.\n\n"
        f"Objective: {goal.objective}\n"
        f"Done when: {goal.done_when}\n\n"
        "PROCEDURE — follow in order, do NOT skip:\n\n"
        "1. DECOMPOSE the 'Done when' text into atomic clauses (independent "
        "requirements joined by AND). Number them. Treat 'X with Y, including "
        "Z' as three clauses (X, Y, Z). An OR within a clause is a single "
        "clause with an alternative — pick the alternative the code actually "
        "shows.\n\n"
        "2. For EACH clause, search the repository for SPECIFIC evidence and "
        "report:\n"
        "   - clause: the exact clause text\n"
        "   - satisfied: yes | no | partial\n"
        "   - evidence: file path(s) + function/class/test name(s) that confirm "
        "satisfaction. 'src/Foo.cs handles it' is NOT evidence; "
        "'src/Foo.cs:42 GetAccountById queries _db.Accounts, covered by "
        "FooTests.GetAccountById_ReturnsAccount' IS evidence.\n"
        "   - if not satisfied or partial: name what is missing and where it "
        "should live (expected file path + symbol).\n\n"
        "3. Reject 'satisfied' based on weak signals — none of these count "
        "alone:\n"
        "   - Tool/symbol NAMES that match the clause (a tool called "
        "`get_accounts` that returns `{\"status\":\"not_yet_available\"}` does "
        "NOT satisfy 'expose accounts to the caller').\n"
        "   - Scaffolding without functionality (an empty contract test that "
        "asserts 'the registry has 16 entries' does NOT satisfy 'tools must "
        "read from real backend data').\n"
        "   - Tests that only assert the stub-like shape (these prove the stub, "
        "not the requirement).\n"
        "   - A merged PR or a passing gate alone — those prove 'behaviour "
        "doesn't break', not 'the requirement is met'.\n\n"
        "4. STRUCTURAL HEALTH — grade the codebase as a senior engineer would. "
        "This is not a checklist of rules; it is professional judgement. "
        "Read the files this goal touched and the folders they live in, and "
        "answer honestly: would you sign off on a PR that left the codebase "
        "in this shape? Look for:\n"
        "   - God objects / files that have absorbed too many responsibilities "
        "(if everything lives in one file because the existing pattern is to "
        "pile everything into one file, the pattern is the smell — not an "
        "excuse).\n"
        "   - Coupled concerns that should be split (types mixed with "
        "implementation; UI mixed with networking; tests piling into a "
        "catch-all spec file because there's nowhere else for them).\n"
        "   - Dead code, no-op stubs, copy-paste, names that don't earn their "
        "length.\n"
        "   - Skipped tests where the comment doesn't match reality, or fixme "
        "markers used to hide work that should have been done.\n"
        "   - Untested behaviour the new code added — green gate is not the "
        "same as covered.\n"
        "   For each concern, name the file:line and what a senior engineer "
        "would do about it. A 'partial' is fine — overall health is a "
        "judgement call. If you would happily hand this codebase to a new "
        "hire on Monday, say so plainly.\n\n"
        "5. Output format — your final report MUST have this structure:\n\n"
        "   ## Per-clause evidence\n"
        "   1. <clause 1 text>\n"
        "      satisfied: yes | no | partial\n"
        "      evidence: <specific files/symbols/tests OR 'missing — should "
        "live in <path>:<symbol>'>\n"
        "   2. <clause 2 text>\n"
        "      satisfied: ...\n"
        "      evidence: ...\n"
        "   ...\n\n"
        "   ## Structural health\n"
        "   verdict: clean | concerns | poor\n"
        "   <one paragraph: would a senior engineer hand this codebase to a "
        "new hire? if not, why not, and which file:lines would they fix first?>\n"
        "   <bullet list of named concerns — file:line + the senior-eng move>\n\n"
        "   ## Summary\n"
        "   <2-3 sentences covering BOTH axes: are all clauses satisfied, AND "
        "is the structural health acceptable? if either is no, the goal is "
        "NOT done — name what the agent must come back and fix>\n\n"
        "   ## Risks not in done_when\n"
        "   <anything worth raising that isn't part of done_when>\n\n"
        "Be honest. You are graded on producing a report that matches reality, "
        "not on appearing thorough. Understating either axis means the goal "
        "will close on work that satisfies the literal clauses but leaves "
        "the codebase worse than it was — which is the failure mode we are "
        "explicitly trying to prevent."
    )


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
    store.save_status(
        goal_id,
        replace(status, lifecycle="executing", phase="blocked", blocked_on=msg, in_flight=None, next=""),
    )
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🟡 [{goal_id}] I couldn't set up the workspace, so I've paused — {msg}",
        summarize=summarize,
    )
    return Outcome.BLOCKED


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
        status = replace(status, last_progress_at=store.now_iso())
        store.save_status(goal_id, status)
        return status
    elapsed = store.seconds_since(status.last_progress_at)
    if elapsed is None or elapsed < window_s or status.no_progress_notified:
        return status
    hours = round(elapsed / 3600, 1)
    status = replace(status, no_progress_notified=True)
    store.save_status(goal_id, status)
    store.append_log(goal_id, f"no-progress watchdog fired — ~{hours}h since last delivery")
    await _notify(
        notifier, NotifyLevel.OWNER,
        f"🐢 [{goal_id}] no progress in ~{hours}h on \"{goal.objective}\" — "
        f"it's still working but nothing has shipped; you may want to take a look",
        summarize=summarize,
    )
    return status


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
    # behaves as "executing"; "investigating" without an in-flight ref is a
    # crash-recovery edge that falls through to executing too (the discovery
    # never resolved; carry on with the bare backlog).
    lifecycle = status.lifecycle or "executing"
    if lifecycle == "investigating":
        return Phase.INVESTIGATING
    if lifecycle == "firming":
        return Phase.FIRMING
    return Phase.EXECUTING


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
    no_progress_s: int = NO_PROGRESS_S,
    decompose_enabled: bool = DECOMPOSE_ENABLED,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    decomposer_caller: "ClaudeCaller | None" = None,
    world_research_caller: "ClaudeCaller | None" = None,
    trend_detector: "object | None" = None,
) -> Outcome:
    """Run one heartbeat and record a single ``tick`` trace event with the
    incoming (lifecycle, phase) and outgoing outcome — the only place the trace
    sees a tick. All the cognition / dispatch / delivery / notify events fired
    during the body land between this tick and the next.

    ``trend_detector`` (typed as ``object`` to avoid an import cycle with
    ``devclaw.trend_detector``): when set, runs per-project trend signals after
    the tick body settles. Telemetry-shaped: a detector exception NEVER breaks
    the tick — it is recorded as a note and swallowed."""
    status_before = store.load_status(goal_id)
    phase_before = _classify(status_before)
    lifecycle_before = status_before.lifecycle or "executing"
    outcome = await _tick_goal_impl(
        goal_id,
        store=store, engine=engine,
        planner_caller=planner_caller, evaluator_caller=evaluator_caller,
        notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
        eval_every=eval_every, verify_done=verify_done, no_progress_s=no_progress_s,
        decompose_enabled=decompose_enabled,
        summary_caller=summary_caller, merger=merger,
        decomposer_caller=decomposer_caller,
        world_research_caller=world_research_caller,
    )
    if trend_detector is not None:
        try:
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
    no_progress_s: int = NO_PROGRESS_S,
    decompose_enabled: bool = DECOMPOSE_ENABLED,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    decomposer_caller: "ClaudeCaller | None" = None,
    world_research_caller: "ClaudeCaller | None" = None,
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
        eval_every=eval_every, verify_done=verify_done, no_progress_s=no_progress_s,
        decompose_enabled=decompose_enabled,
        summary_caller=summary_caller, merger=merger,
        decomposer_caller=decomposer_caller,
        world_research_caller=world_research_caller,
    )

    # Effective goal = goal.yaml overlaid with firmed.yaml outputs when firming
    # has landed. Every cognition + gating path inside this tick (planner,
    # evaluator, done-gate) reads from here, so firmed stub_acceptable / derived
    # done_when are honored — not silently shadowed by the original goal.yaml.
    goal = store.load_effective_goal(goal_id)
    status = store.load_status(goal_id)
    phase = _classify(status)

    # Terminal short-circuit — skip even the watchdog.
    if phase is Phase.TERMINAL_DONE:
        return Outcome.SKIP_DONE
    if phase is Phase.TERMINAL_CANCELLED:
        return Outcome.SKIP_CANCELLED

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


async def _auto_deploy(goal_id: str, goal: Goal, store: GoalStore) -> str:
    """Deploy the built app to a durable Tailscale URL on goal completion and return
    a short suffix to append to the completion notice (the live URL, or empty). Fully
    best-effort: any failure is logged and swallowed — a verified-complete goal must
    never be reopened because hosting wobbled. Off via DEVCLAW_GOAL_AUTODEPLOY=0."""
    if os.environ.get("DEVCLAW_GOAL_AUTODEPLOY", "1") == "0":
        return ""
    try:
        out = await _deploy.deploy_project(goal.workspace_dir, goal_id)
    except Exception as exc:  # noqa: BLE001 — handoff is a bonus; completion is not contingent on it
        store.append_log(goal_id, f"auto-deploy skipped: {exc}")
        return ""
    url = out.get("url") or out.get("loopback_url", "")
    store.append_log(goal_id, f"deployed: {url or out.get('container')} (ready={out.get('ready')})")
    if out.get("tailscale_served") and out.get("url"):
        return f"\n🔗 live: {out['url']}"
    # Tailscale not wired from here — hand back the one-time serve command.
    return f"\n🔗 deployed on the VPS — expose once: {out.get('serve_command', '')}"


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
        # Handoff: a completed goal should be a thing the owner can OPEN, not just a
        # closed ticket. Best-effort deploy the built app to a durable Tailscale URL.
        # NEVER let a deploy hiccup undo a verified-complete goal — the goal IS done.
        live = await _auto_deploy(goal_id, goal, store)
        await _notify(notifier, NotifyLevel.OWNER, f"✅ [{goal_id}] goal complete (verified) — {ev.rationale[:200]}{live}", summarize=summarize)
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
        # In checklist mode the done-gate reviewer needs to see the goal's
        # accumulated work — read the goal branch, not the default branch
        # (otherwise it judges done_when against an empty diff).
        done_gate_branch = f"goal/{goal_id}" if store.read_checklist(goal_id) else None
        try:
            await prepare_ws(goal.workspace_dir, goal.repo_url, done_gate_branch, goal.skills_required)
        except WorkspaceError as exc:
            store.append_log(goal_id, f"done-gate workspace prep failed: {exc}")
            store.save_status(goal_id, replace(base, phase="idle", next="retry done-gate"))
            await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] done-gate workspace prep failed: {exc}")
            return Outcome.ERROR
        review = Action(
            engine="devclaw", tool="review_repository",
            goal=_done_gate_review_brief(goal),
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
        _trace.record_dispatch(goal_id=goal_id, tool=review.tool, ref_id=ref.id, engine=getattr(engine, "kind", ""), is_done_check=True)
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
    store.save_status(
        goal_id, replace(status, lifecycle=next_lifecycle, phase="idle", next=next_note),
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
    try:
        ref = await engine.dispatch(review, goal, notify_url)
    except Exception as exc:  # noqa: BLE001
        store.append_log(goal_id, f"investigation dispatch failed ({exc}) — skipping to executing")
        store.save_status(goal_id, replace(status, lifecycle="executing", phase="idle"))
        return Outcome.SLEPT
    ref = replace(ref, is_discovery=True)
    _trace.record_dispatch(goal_id=goal_id, tool=review.tool, ref_id=ref.id, engine=getattr(engine, "kind", ""), is_discovery=True)
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
    store.save_status(
        goal_id, replace(status, lifecycle=next_lifecycle, phase="idle", next=next_phase_note),
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


def _flag_items_in_flight(store: GoalStore, goal_id: str, item_ids: list[str]) -> None:
    """When a dispatched action carries ``addresses``, mark those checklist
    items ``in_flight`` so the planner's ``ready_items`` filter excludes them
    on the next tick (no re-pick of the same item before settle). No-op when
    no checklist exists or the action has no addresses."""
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
            )
    store.write_checklist(goal_id, updated)


def _settle_addressed_items(
    store: GoalStore, goal_id: str, addresses: list[str], poll: PollResult,
) -> None:
    """Settle the checklist items an action was addressing. Successful task
    (poll.status == 'done' AND gate_passed in {None, True}) flips items to
    ``done`` with grounded evidence (PR url + gate verdict); a failed task
    flips them back to ``not_started`` so the planner can re-pick them next
    tick (sharper instruction, different angle, or eventually block + ask).
    The per-item gate (review_gate) verifies the diff against
    ``evidence_target`` separately — session 4."""
    if not addresses:
        return
    current = store.read_checklist(goal_id)
    if current is None:
        return
    success = poll.status == "done" and (poll.gate_passed is None or poll.gate_passed)
    if success:
        ev_parts: list[str] = []
        if poll.pr_url:
            ev_parts.append(f"PR {poll.pr_url}")
        if poll.gate_passed is not None:
            ev_parts.append("gate=passed" if poll.gate_passed else "gate=FAILED")
        evidence = " · ".join(ev_parts) or "settled (no PR or gate)"
        new_status = "done"
    else:
        # leave evidence None — the item is back in the pick-pool, not yet proven
        evidence = None
        new_status = "not_started"
    updated = current
    for item_id in addresses:
        try:
            updated = _checklist.update_item(
                updated, item_id, status=new_status, evidence=evidence,
            )
        except KeyError:
            continue
    store.write_checklist(goal_id, updated)


async def _dispatch_action(
    goal_id: str, goal: Goal, base: GoalStatus, action: Action,
    *, store: GoalStore, engine: GoalEngine, notifier: Notifier,
    notify_url: str, prepare_ws: WorkspacePrep,
    summarize: "ClaudeCaller | None" = None,
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
    base_cap = len(goal.backlog) + 2
    checklist = store.read_checklist(goal_id)
    cap = max(base_cap, len(checklist.items) + 2) if checklist else base_cap
    if base.actions_dispatched >= cap:
        store.append_log(goal_id, f"dispatch cap {cap} reached — blocking for review")
        store.save_status(
            goal_id,
            replace(base, phase="blocked", blocked_on=f"dispatch cap {cap} reached — review the open PRs"),
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
    if action.tool != "review_repository" and store.read_checklist(goal_id):
        branch_for_dispatch = f"goal/{goal_id}"
    try:
        await prepare_ws(goal.workspace_dir, goal.repo_url, branch_for_dispatch, goal.skills_required)
    except WorkspaceError as exc:
        return await _block_on_prep_failure(
            goal_id, base, exc, store=store, notifier=notifier, summarize=summarize,
        )
    try:
        ref = await engine.dispatch(action, goal, notify_url)
    except Exception as exc:  # noqa: BLE001 — record + notify, retry next cadence
        store.append_log(goal_id, f"dispatch error ({action.tool}): {exc}")
        store.save_status(goal_id, replace(base, phase="idle", next=action.goal))
        await _notify(notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] dispatch failed: {exc}")
        return Outcome.ERROR
    # Carry the action's checklist addresses onto the in-flight ref so the
    # settle hook can update the right items without re-reading the plan.
    if action.addresses:
        ref = replace(ref, addresses=list(action.addresses))
    _trace.record_dispatch(goal_id=goal_id, tool=action.tool, ref_id=ref.id, engine=getattr(engine, "kind", ""))
    store.save_status(
        goal_id,
        replace(
            base, phase="in_flight", in_flight=ref, blocked_on=None, next=action.goal,
            actions_dispatched=base.actions_dispatched + 1,
        ),
    )
    # Checklist mode: flip addressed items to in_flight so the planner doesn't
    # re-pick them next tick before this one settles. No-op in legacy mode.
    _flag_items_in_flight(store, goal_id, list(action.addresses))
    store.append_log(goal_id, f"dispatched {action.tool}: {action.goal} → {ref.id}")
    # Notify uses the short label, not the full prompt body — the raw `action.goal`
    # is a multi-paragraph executor instruction (often 500-1500 chars) and dumping
    # it to Telegram floods the owner with prompt boilerplate. Full text stays in
    # log.md above for forensic readability.
    await _notify(
        notifier, NotifyLevel.TASK,
        f"🚀 [{goal_id}] {action.tool}: {_action_label(action)}",
    )
    return Outcome.DISPATCHED


# ---- registry-driven phase dispatch ----------------------------------------


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
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
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


# ---- phase handlers --------------------------------------------------------
# One handler per Phase value. Each takes (goal_id, goal, status, ctx) — except
# the polling handlers, which the orchestrator calls with status already loaded
# — and returns either an :class:`Outcome` (terminal for this tick) or, for
# ``_resolve_polling_action``, an ``(updated_status, finished_detail)`` tuple
# so the EXECUTING handler can chain on the same tick.


async def _resolve_polling_discovery(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> Outcome:
    """Settle an in-flight discovery review. Still running → IN_FLIGHT. Else
    record the review outcome, clear in_flight, and synthesize the brief via
    :func:`_resolve_discovery`."""
    ref = status.in_flight
    poll = await ctx.engine.poll(ref)
    if poll.running:
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
        return Outcome.IN_FLIGHT
    ctx.store.append_log(goal_id, f"discovery review {ref.id} → {poll.status}")
    discovery_detail = poll.detail or f"review {poll.status} (no analysis captured)"
    new_status = replace(status, in_flight=None, phase="idle")
    # Persist BEFORE the synthesis call (which may raise on a usage limit) so a
    # later crash can't rewind to "still in-flight" and re-poll the same ref.
    ctx.store.save_status(goal_id, new_status)
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
    ``done_when`` via :func:`_resolve_done_gate`."""
    ref = status.in_flight
    poll = await ctx.engine.poll(ref)
    if poll.running:
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
        return Outcome.IN_FLIGHT
    ctx.store.append_log(goal_id, f"done-check review {ref.id} → {poll.status}")
    review_report = poll.detail or f"review {poll.status} (no report captured)"
    new_status = replace(status, in_flight=None, phase="idle")
    ctx.store.save_status(goal_id, new_status)
    return await _resolve_done_gate(
        goal_id, goal, new_status, review_report,
        store=ctx.store, evaluator_caller=ctx.evaluator_caller, notifier=ctx.notifier,
        summarize=ctx.summary_caller,
    )


async def _resolve_polling_action(
    goal_id: str, goal: Goal, status: GoalStatus, ctx: TickContext,
) -> "Union[Outcome, Tuple[GoalStatus, str]]":
    """Settle an in-flight regular action. Still running → IN_FLIGHT.
    Otherwise: record the delivery (grounded evidence for the evaluator), update
    the no-progress watchdog, run auto-merge if enabled, persist the cleared
    state IMMEDIATELY (protects against the duplicate-merge loop dogfooded
    2026-06-21), and return ``(new_status, finished_detail)`` so the EXECUTING
    handler can plan the next action on the same tick with the just-finished
    detail in hand."""
    ref = status.in_flight
    poll = await ctx.engine.poll(ref)
    if poll.running:
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
        return Outcome.IN_FLIGHT

    evidence = []
    if poll.pr_url:
        evidence.append(f"PR {poll.pr_url}")
    if poll.gate_passed is not None:
        evidence.append("gate=passed" if poll.gate_passed else "gate=FAILED")
    ev_str = (" — " + ", ".join(evidence)) if evidence else ""

    ctx.store.append_log(goal_id, f"{ref.tool} {ref.id} → {poll.status}{ev_str}")
    ctx.store.append_delivery(goal_id, ref.goal or ref.tool, poll.detail or "")
    # Checklist mode: settle the items this action was addressing — success
    # flips them to done with grounded evidence (PR + gate), failure flips
    # them back to not_started so the planner can re-pick them.
    if getattr(ref, "addresses", None):
        _settle_addressed_items(ctx.store, goal_id, list(ref.addresses), poll)
    _trace.record_delivery(
        goal_id=goal_id, action_label=_action_label(ref),
        gate_passed=poll.gate_passed, pr_url=poll.pr_url or "",
    )
    finished_detail = f"tool={ref.tool} id={ref.id} status={poll.status}{ev_str}\n{poll.detail}"

    delivered = 1 if poll.status == "done" else 0
    new_status = replace(
        status, in_flight=None, phase="idle",
        deliveries_since_eval=status.deliveries_since_eval + delivered,
        # a delivery is forward progress → reset the no-progress watchdog.
        last_progress_at=(ctx.store.now_iso() if delivered else status.last_progress_at),
        no_progress_notified=(False if delivered else status.no_progress_notified),
    )

    # Hands-off auto-merge: a delivered change whose verify gate passed is
    # merged by devclaw itself, with a plain owner ping. Best-effort + gated —
    # a failed merge just leaves the PR for review.
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
    in_checklist_dispatch = bool(getattr(ref, "addresses", None))
    if (
        _merge.AUTOMERGE_ENABLED and ctx.merger is not None
        and poll.status == "done" and poll.gate_passed and poll.pr_url
        and not in_checklist_dispatch
    ):
        if await ctx.merger(poll.pr_url):
            ctx.store.append_log(goal_id, f"auto-merged {poll.pr_url}")
            await _notify(
                ctx.notifier, NotifyLevel.OWNER,
                f"✅ [{goal_id}] shipped + merged — {_action_label(ref)} ({poll.pr_url})",
                summarize=ctx.summary_caller,
            )
        else:
            ctx.store.append_log(goal_id, f"auto-merge failed, left for review: {poll.pr_url}")

    # Persist IMMEDIATELY — the next-action planner can raise on a usage limit;
    # if the cleared state isn't durable first the tick aborts with in_flight
    # still pointing at the just-finished action and the next tick re-ships it
    # (duplicate-merge loop, dogfood 2026-06-21).
    ctx.store.save_status(goal_id, new_status)
    return new_status, finished_detail


async def _handle_executing(
    goal_id: str, goal: Goal, status: GoalStatus, finished_detail: str, ctx: TickContext,
) -> Outcome:
    """The cognition path. Gate by work-present + cadence (preserves the
    zero-token guard — blocked goals only unblock on real work, never on the
    timer), then optionally run a periodic direction eval, then plan one
    action and dispatch on the planner's decision."""
    steering = ctx.store.unread_steering(goal_id, status)
    work = bool(finished_detail) or bool(steering)
    if status.phase == "blocked":
        should_plan = work  # cadence does NOT re-poke a blocked goal; only work unblocks
    else:
        should_plan = work or ctx.store.cadence_due(goal, status)
    if not should_plan:
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
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
        steering = ctx.store.unread_steering(goal_id, status)  # re-read

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
    try:
        result = await _planner.plan(
            goal, status, ctx.store.recent_log(goal_id), steering, finished_detail,
            claude_caller=ctx.planner_caller, discovery=ctx.store.read_discovery(goal_id),
            checklist=ctx.store.read_checklist(goal_id),
            trends=trends_text,
        )
    except (_planner.GoalPlannerError, PlannerError) as exc:
        # A usage/rate limit at the PLANNER must pause the layer, not be logged
        # and retried next tick (re-burning quota). The goal planner shells out
        # via the shared claude --print caller, which raises PlannerError on a
        # non-zero exit (e.g. a session limit). Catch BOTH (dogfood 2026-06-21).
        paused = _maybe_pause(ctx.engine, ctx.store, goal_id, str(exc))
        if paused is not None:
            ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
            return paused
        ctx.store.append_log(goal_id, f"plan error: {exc}")
        ctx.store.save_status(goal_id, replace(status, last_tick_at=ctx.store.now_iso()))
        await _notify(ctx.notifier, NotifyLevel.TASK, f"⚠️ [{goal_id}] plan step failed: {exc}")
        return Outcome.ERROR

    now = ctx.store.now_iso()
    base = replace(
        status, last_plan_at=now, last_tick_at=now,
        inbox_cursor=ctx.store.steering_cursor(goal_id),  # all current steering consumed
    )

    if result.decision == "sleep":
        ctx.store.save_status(goal_id, replace(base, phase="idle", next=result.note))
        ctx.store.append_log(goal_id, f"sleep: {result.note}")
        return Outcome.SLEPT

    if result.decision == "blocked":
        ctx.store.save_status(goal_id, replace(base, phase="blocked", blocked_on=result.question, next=""))
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
        )

    # decision == "act"
    return await _dispatch_action(
        goal_id, goal, base, result.actions[0],
        store=ctx.store, engine=ctx.engine, notifier=ctx.notifier,
        notify_url=ctx.notify_url, prepare_ws=ctx.prepare_ws, summarize=ctx.summary_caller,
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
    no_progress_s: int = NO_PROGRESS_S,
    summary_caller: "ClaudeCaller | None" = None,
    merger: "_merge.Merger | None" = None,
    tracer_factory: "Callable[[str], _trace.Tracer | None] | None" = None,
    trend_detector: "object | None" = None,
) -> dict[str, Outcome]:
    """Tick every goal. One goal's failure never stops the others, and a usage
    limit pauses the whole layer (0 tokens) rather than crashing per-goal.

    ``tracer_factory(goal_id) -> Tracer | None`` is the seam GoalService uses
    to attach a :class:`PersistentTracer` per goal-tick so the cascade's
    cognition / dispatch / delivery events land in the durable trace store.

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
    if until:
        if _now_ms() < until:
            return {gid: Outcome.RATE_LIMITED for gid in store.list_goal_ids()}
        _engine_clear_pause(engine)

    for goal_id in store.list_goal_ids():
        tracer = tracer_factory(goal_id) if tracer_factory else None
        try:
            with _trace.tracer_scope(tracer):
                outcomes[goal_id] = await tick_goal(
                    goal_id, store=store, engine=engine,
                    planner_caller=planner_caller, evaluator_caller=evaluator_caller,
                    notifier=notifier, notify_url=notify_url, prepare_ws=prepare_ws,
                    eval_every=eval_every, verify_done=verify_done, no_progress_s=no_progress_s,
                    summary_caller=summary_caller, merger=merger,
                    trend_detector=trend_detector,
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


def _engine_clear_pause(engine: GoalEngine) -> None:
    fn = getattr(engine, "clear_global_pause", None)
    if callable(fn):
        fn()


def _maybe_pause(engine: GoalEngine, store: GoalStore, goal_id: str, err: str) -> "Outcome | None":
    """If ``err`` is a usage/rate-limit, set the shared quota pause and return
    Outcome.RATE_LIMITED; otherwise None (the caller handles it as a real error).
    Centralizes the goal-side quota guard so every cognition call can use it."""
    cls = classify_failure(err)
    if not (cls.is_pausing and hasattr(engine, "set_global_pause")):
        return None
    backoff = pause_seconds(cls.retry_after_s)
    engine.set_global_pause(_now_ms() + backoff * 1000, f"{cls.kind.value} (goal cognition)")
    store.append_log(goal_id, f"paused — {cls.kind.value}; resuming in ~{backoff}s")
    return Outcome.RATE_LIMITED
