"""Async task executor — DB-driven, crash-safe, heartbeat-paced.

The MCP handler calls ``submit()`` / ``submit_program()`` and gets an id back
immediately; engine runs happen in the background and the state store is flipped
when they settle. Single-writer-to-state by design — only this queue mutates rows.

**Scheduling is reconciled from DB state, not from in-memory counters.** The core
is :meth:`_pump`: read what's runnable from the store, claim it atomically, launch
up to the concurrency caps. Three things call it — ``submit``, a task settling,
and a periodic **heartbeat tick** — and they're all idempotent because
``claim_pending`` is the final guard. Because concurrency is derived from the
``running`` rows (not a counter that dies with the process), the system is
**crash-safe**: :meth:`recover` resets orphaned ``running`` tasks at startup and
the next pump resumes them. That's the "ephemeral body / durable mind" model — a
build survives restarts.

**Cheap-idle guard:** every pump first asks the store "is there any work?" (one
COUNT) and returns immediately if not — an idle tick costs ~nothing, so we never
burn the engine on empty ticks.

Programs (DAGs): a program is planned (the planner → tasks with deps), then each
pump schedules ready tasks (deps all ``done``) up to the per-program + global
caps. A single sibling failure makes the program "sticky failed" — pending
siblings don't start, in-flight ones run to completion, then the program fails.

Notifications: standalone tasks fire their own ``notify_url`` on terminal state
(bounded retries); program-child tasks don't — only the program-level notify
fires once the program terminates (one program in, one notify out).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .delivery import deliver_change, delivery_failed
from .engine import Engine, EngineEvent, EngineRequest
from .loom.limits import classify_failure, pause_seconds
from .loom.test_integrity import scan_diff
from .planner import PlannedTask, PlannerError, plan_goal
from .quality import format_feedback, review_diff
from .engine.sandcastle import run_sandcastle, sweep_orphan_sandboxes
from .dispatch_gate import operator_block
from .state_store import Program, StateStore, Task, TaskKind, _now_ms
# Leaf concerns split out of this module. The git ``_sync`` helpers are re-exported
# here because tests import them from ``task_queue`` and patch ``_wip_snapshot_sync``
# on this namespace; the async wrappers below look them up as module globals.
from .task_git import _git_diff_sync, _git_head_sync, _wip_snapshot_sync  # noqa: F401
from .task_notify import _NotifyMixin

NOTIFY_BACKOFF_MS = (1000, 2000, 4000)
MAX_CONCURRENT_PER_PROGRAM = int(os.environ.get("DEVCLAW_MAX_CONCURRENT_PER_PROGRAM", "2"))
#: global cap on concurrently-running tasks across all programs — backpressure
GLOBAL_MAX_CONCURRENT = int(os.environ.get("DEVCLAW_MAX_CONCURRENT", "4"))
#: heartbeat interval — the tick re-derives scheduling from DB state
TICK_SECONDS = float(os.environ.get("DEVCLAW_TICK_SECONDS", "10"))
#: per-task wall-clock cap (seconds). A run that exceeds it is cancelled — which
#: tears down its sandbox via run_sandcastle's finally — and the task is marked
#: failed, so a hung agent fails CLEANLY instead of burning Pro/Max quota forever
#: (the live smoke leaked a container on exactly this — a silent post-init hang).
#: It's a coarse backstop: a no-progress timer would kill a silent hang faster,
#: but this also catches busy-loops. <=0 disables. Generous default so a
#: legitimately long feature build isn't reaped mid-flight — 1800s proved NOT
#: generous enough for real program work (2026-07-09: a mid-stack closeloop
#: implement_feature doing honest work was reaped at 30min, failing the whole
#: program).
TASK_TIMEOUT_S = float(os.environ.get("DEVCLAW_TASK_TIMEOUT_S", "3600"))
#: how many times to RE-RUN a task that fails its verify gate (or errors), each
#: time with the failure fed back into the goal, before escalating. The gate
#: catches a bad result; retry gives the agent a bounded second chance to
#: self-correct (a fix that didn't fully land, a transient error). 0 disables.
#: NOT applied to timeouts — a stuck run would likely just hang again.
TASK_MAX_RETRIES = int(os.environ.get("DEVCLAW_MAX_RETRIES", "1"))
#: the pre-PR adversarial review gate: after the verify gate + test-integrity
#: pass (behaviour is proven), a Claude pass READS the diff against the ticket +
#: the quality bar and can send it back through the retry loop (request_changes)
#: BEFORE the PR opens — closing the "green but untrustworthy" hole a spectator-PO
#: can't see. On by default (it costs one Claude call per successful code
#: task); a project may opt out via its registry `review_gate` override.
REVIEW_GATE_ENABLED = True
#: review applies only to code-producing kinds (a diff to read); review_repository
#: is read-only and onboard writes only a comprehension doc.
_REVIEWABLE_KINDS = ("implement_feature", "fix_bug")

#: Stable marker prefixed on the feedback string when the review gate CRASHED —
#: it couldn't produce a verdict at all (e.g. an oversized/unparseable diff makes
#: the review model return non-JSON). The retry loop treats this differently from
#: a genuine ``request_changes``: a crash is not a defect the agent can fix by
#: re-running (the same diff re-crashes the gate identically), so it fails FAST
#: instead of burning the retry budget and then the goal-level re-dispatch loop.
_REVIEW_CRASH_MARKER = "review gate crashed (failing closed):"
#: per-workspace circuit-breaker: N task failures on the same workspace_dir
#: within WINDOW_S trips a hold for HOLD_S. Sibling of the global quota pause but
#: scoped, so one broken workspace doesn't starve the others. Trigger event that
#: named this: 2026-07-02 closeloop retry storm — 6+ duplicate dispatches racing
#: on the same repo burned quota with zero PR output because per-task retries
#: alone don't stop a workspace-level defect. Threshold <=0 disables.
WORKSPACE_BREAK_THRESHOLD = 3
WORKSPACE_BREAK_WINDOW_S = 900.0
WORKSPACE_BREAK_HOLD_S = 1800.0
#: how many usage-limit pause→requeue cycles a single task gets before it is
#: FAILED instead of requeued again. A permanently-failing task whose error text
#: happens to match the quota/rate regexes would otherwise loop pause→requeue→
#: re-run forever — the workspace breaker never sees it (a paused task never
#: becomes a `failed` row). The global pause is still set either way: the
#: account really is limited; only the doomed task stops riding it.
MAX_PAUSE_REQUEUES = 5

#: _run_and_settle returns this when a task was paused for a quota limit (not
#: settled): the task is back to 'pending' and the global pause holds dispatch.
_PAUSED = object()

PlannerFn = Callable[[str, str], Awaitable[list[PlannedTask]]]
#: the execution engine — orchestration depends on this seam, not on OpenHands
RunnerFn = Engine


def _verify_failure_summary(verify: dict) -> str:
    """Human-readable failure reason for a task whose verify gate didn't pass —
    stored as the task error so a human (or a retry) can see what broke."""
    cmd = verify.get("cmd", "")
    if verify.get("timed_out"):
        head = f"verify gate timed out: `{cmd}`"
    else:
        head = f"verify gate failed (exit {verify.get('exit_code')}): `{cmd}`"
    out = (verify.get("output") or "").strip()
    return f"{head}\n{out[-1500:]}" if out else head


async def _git_diff(host_dir: str, base: str = "") -> str:
    """Async wrapper — runs the blocking git diff in a thread so it never blocks
    the event loop or trips the asyncio-subprocess child-watcher hang. Looks up
    :func:`_git_diff_sync` as a module global so tests can patch it here."""
    return await asyncio.to_thread(_git_diff_sync, host_dir, base)


async def _git_head(host_dir: str) -> str:
    """Async wrapper — same thread-offload rationale as :func:`_git_diff`."""
    return await asyncio.to_thread(_git_head_sync, host_dir)


async def _wip_snapshot(host_dir: str, task_id: str) -> str:
    """Async wrapper — same thread-offload rationale as :func:`_git_diff`."""
    return await asyncio.to_thread(_wip_snapshot_sync, host_dir, task_id)


def _integrity_failure(diff: str) -> Optional[str]:
    """Return a failure summary if the change weakened the tests (deleted/skipped),
    else None. Enforces what the prompt only asks for. Operates on an already-
    computed diff (shared with the review gate). A CRASH in the scanner fails
    CLOSED: a quality gate that silently no-ops on its own error is exactly how
    a gutted test suite ships unnoticed — the crash feeds the same retry loop
    as a real integrity failure, then escalates."""
    try:
        report = scan_diff(diff)
    except Exception as err:  # noqa: BLE001 — fail closed, never silently approve
        return (
            f"test-integrity gate crashed (failing closed): "
            f"{err.__class__.__name__}: {err}. The change was not scanned for "
            "weakened tests, so it must not ship on the gate's silence."
        )
    if report.ok:
        return None
    return (
        f"{report.summary()}. The gate passed, but the change weakened the test "
        "suite — restore the tests and make the code genuinely pass them; do not "
        "delete, skip, or gut tests to go green."
    )


class TaskQueue(_NotifyMixin):
    @staticmethod
    def _derive_engine_kind(runner: "RunnerFn") -> str:
        """Map a runner function to a short label for the trace ("stub" /
        "sandcastle" / "host" / "claude_sdk"). Falls back to the function's
        qualified name so unknown custom runners are still identifiable."""
        qualname = getattr(runner, "__qualname__", "") or getattr(runner, "__name__", "")
        if "run_sandcastle" in qualname:
            return "sandcastle"
        if "run_host" in qualname:
            return "host"
        if "run_claude_sdk" in qualname:
            return "claude_sdk"
        if "stub_engine" in qualname or qualname.startswith("stub"):
            return "stub"
        return qualname or "unknown"

    @property
    def engine_kind(self) -> str:
        return self._engine_kind

    def __init__(
        self,
        store: StateStore,
        planner: Optional[PlannerFn] = None,
        runner: Optional[RunnerFn] = None,
        on_settle: Optional[Callable[[], None]] = None,
        reviewer: Optional[Callable[..., Awaitable[dict]]] = None,
    ) -> None:
        self._store = store
        # Injectable for tests — default to the real planner / sandcastle runner.
        self._planner: PlannerFn = planner or (lambda g, w: plan_goal(g, w))
        self._runner: RunnerFn = runner or run_sandcastle
        # A short engine-kind label for trace events ("stub" / "sandcastle" /
        # "host" / "claude_sdk") — derived from the runner's qualified name so
        # silently mis-wired sandboxes can be spotted in the timeline.
        self._engine_kind: str = self._derive_engine_kind(self._runner)
        # The pre-PR review gate's cognition (diff → verdict). Injectable so tests
        # stub the Claude call; defaults to the real review_diff (host-side claude).
        self._reviewer: Callable[..., Awaitable[dict]] = reviewer or review_diff
        # Optional in-process hook fired whenever a task/program reaches a
        # terminal state — the goal layer wires its heartbeat-wake here so a
        # finished engine run triggers an immediate goal tick (replacing the old
        # cross-service HTTP /wake). Must be cheap + non-throwing.
        self._on_settle: Optional[Callable[[], None]] = on_settle
        #: retain background task refs so they aren't garbage-collected mid-run
        self._bg: set[asyncio.Task] = set()
        #: task_id -> the live asyncio.Task running its engine, so cancel() can
        #: reach in and tear a specific run down (the docker subprocess dies via
        #: the runner's finally). Only ever holds genuinely in-flight tasks.
        self._running_tasks: dict[str, asyncio.Task] = {}
        #: program_ids whose planner is in flight — guards against double-plan
        self._planning: set[str] = set()
        #: the heartbeat tick task (started by the server, not in tests)
        self._tick_task: Optional[asyncio.Task] = None
        #: optional project registry, wired post-construction (see set_registry) —
        #: used ONLY to resolve a per-project review_gate override. None is fine:
        #: the gate falls back to the devclaw-wide REVIEW_GATE_ENABLED default.
        self._registry: Optional[object] = None

    def set_on_settle(self, hook: Optional[Callable[[], None]]) -> None:
        """Register the terminal-state hook (the goal layer's heartbeat wake)."""
        self._on_settle = hook

    def set_registry(self, registry: Optional[object]) -> None:
        """Wire the project registry after construction (the registry is built
        after the queue in server/_state.py). Used only to resolve the
        per-project ``review_gate`` override."""
        self._registry = registry

    def _fire_settle(self) -> None:
        if self._on_settle is not None:
            try:
                self._on_settle()
            except Exception as err:  # noqa: BLE001 — a bad hook must never break a run
                sys.stderr.write(f"task-queue: on_settle hook failed: {err}\n")

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _workspace_break_active(self, workspace_dir: str) -> bool:
        """True iff dispatch to ``workspace_dir`` is currently held by the
        circuit-breaker. Auto-clears an expired break so the meta table doesn't
        grow with dead keys — same lazy-clear the global pause uses."""
        until, _ = self._store.get_workspace_break(workspace_dir)
        if until == 0:
            return False
        if _now_ms() < until:
            return True
        self._store.clear_workspace_break(workspace_dir)
        return False

    def _check_and_trip_breaker(self, workspace_dir: str, task_id: str) -> None:
        """Called after a task failure. If the workspace has now crossed the
        threshold within the sliding window AND no break is already active, trip
        one and emit a breaker event. One-shot per hold — subsequent failures
        during an active break don't re-fire (avoids notify spam)."""
        if WORKSPACE_BREAK_THRESHOLD <= 0:
            return
        if self._workspace_break_active(workspace_dir):
            return  # already tripped — the hold is running
        since_ms = _now_ms() - int(WORKSPACE_BREAK_WINDOW_S * 1000)
        count = self._store.count_recent_task_failures(workspace_dir, since_ms)
        if count < WORKSPACE_BREAK_THRESHOLD:
            return
        until_ms = _now_ms() + int(WORKSPACE_BREAK_HOLD_S * 1000)
        reason = (
            f"circuit-breaker: {count} task failures in "
            f"{WORKSPACE_BREAK_WINDOW_S:.0f}s on {workspace_dir}"
        )
        self._store.set_workspace_break(workspace_dir, until_ms, reason)
        self._store.append_event(
            task_id=task_id,
            program_id=None,
            type="workspace_break_tripped",
            source="devclaw",
            payload_json=json.dumps({
                "workspace_dir": workspace_dir,
                "count": count,
                "window_s": WORKSPACE_BREAK_WINDOW_S,
                "hold_s": WORKSPACE_BREAK_HOLD_S,
                "until_ms": until_ms,
                "reason": reason,
            }),
        )
        sys.stderr.write(
            f"task-queue: workspace break tripped for {workspace_dir} "
            f"({count} failures in {WORKSPACE_BREAK_WINDOW_S:.0f}s) — "
            f"holding dispatch {WORKSPACE_BREAK_HOLD_S:.0f}s\n"
        )

    # ---- cancellation (deliberate abort) --------------------------------

    def _abort_live_task(self, task_id: str) -> None:
        """Tear down the in-flight execution of one task, if any. The DB row must
        already be 'cancelled' (terminal) BEFORE this — so the CancelledError that
        propagates out of the run can't be re-settled as 'failed' (mark_failed
        guards on pending/running). The runner's finally kills the container."""
        task = self._running_tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()

    def cancel_task(self, task_id: str) -> bool:
        """Abort one task. Marks it 'cancelled' (no-op if already terminal), then
        tears down its live run. If the task belongs to a program, the program is
        sticky-cancelled on the next pump (a hole in the DAG blocks its dependents).
        Returns True iff the task was actually pending/running (i.e. abortable)."""
        moved = self._store.mark_task_cancelled(task_id)
        if not moved:
            return False  # already done/failed/cancelled — nothing to abort
        task = self._store.get_task(task_id)
        self._store.append_event(
            task_id=task_id,
            program_id=task.program_id if task else None,
            type="cancelled",
            source="devclaw",
            payload_json=json.dumps({"reason": "cancelled by client"}),
        )
        self._abort_live_task(task_id)
        # A slot may have freed (standalone) or the program now needs to
        # terminalize as cancelled — reconcile.
        self._pump()
        return True

    def cancel_program(self, program_id: str) -> bool:
        """Abort a whole program: cancel its pending tasks (so nothing new starts),
        tear down every running task, and mark the program 'cancelled'. Returns
        True iff the program was non-terminal (i.e. abortable)."""
        program = self._store.get_program(program_id)
        if program is None or program.status in ("done", "failed", "cancelled"):
            return False
        # Stop scheduling first, then drain in-flight work.
        cancelled_pending = self._store.cancel_program_pending_tasks(program_id)
        running = [
            t.id
            for t in self._store.list_program_tasks(program_id)
            if t.status == "running"
        ]
        for tid in running:
            self._store.mark_task_cancelled(tid)
            self._abort_live_task(tid)
        self._store.mark_program_cancelled(program_id, error="cancelled by client")
        for tid in cancelled_pending + running:
            self._store.append_event(
                task_id=tid,
                program_id=program_id,
                type="cancelled",
                source="devclaw",
                payload_json=json.dumps({"reason": "program cancelled by client"}),
            )
        # Cancelling freed global concurrency slots (the running rows are now
        # terminal) — let other pending work / programs claim them.
        self._pump()
        return True

    async def drain(self) -> None:
        """Await all in-flight background work. Used by tests for determinism.

        The ``sleep(0)`` is load-bearing: when every task in ``_bg`` is already
        done, ``gather`` resolves without yielding to the loop, so the
        ``add_done_callback`` discards never run and ``_bg`` never shrinks.
        Yielding once lets those call_soon callbacks fire so the loop ends.
        """
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
            await asyncio.sleep(0)

    # ---- submission -----------------------------------------------------

    def submit(
        self,
        *,
        kind: TaskKind,
        workspace_dir: str,
        goal: str,
        notify_url: Optional[str] = None,
        verify_cmd: Optional[str] = None,
        deliver: bool = False,
        title: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
        scaffold: bool = False,
        pump: bool = True,
    ) -> str:
        """Create a task row (status 'pending') and, by default, immediately
        reconcile execution against it (claim + launch, up to the caps).

        ``pump=False`` (PR7 — the dispatch/pump split): create the row ONLY,
        no claim, no launch. ``_pump()`` synchronously claims PENDING work —
        including UNRELATED tasks — and spawns real ``asyncio`` execution for
        it; a caller that wraps ``submit()`` in its own atomic unit (the goal
        heartbeat's dispatch transaction) cannot let that unit's eventual
        rollback leave a phantom container running against a row that no
        longer exists. ``pump=False`` callers are responsible for pumping
        later (``pump()``/``kick()``, or simply the queue's own periodic
        ``start_ticking`` heartbeat, which self-heals a missed pump within
        one ``TICK_SECONDS``)."""
        task_id = str(uuid.uuid4())
        self._store.create_task(
            id=task_id,
            kind=kind,
            workspace_dir=workspace_dir,
            goal=goal,
            notify_url=notify_url,
            verify_cmd=verify_cmd,
            deliver=deliver,
            title=title,
            parent_goal_id=parent_goal_id,
            scaffold=scaffold,
        )
        if pump:
            self._pump()
        return task_id

    def submit_program(
        self,
        *,
        workspace_dir: str,
        goal: str,
        notify_url: Optional[str] = None,
        open_pr: bool = False,
        verify_cmd: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
        pump: bool = True,
    ) -> str:
        """Submit a program the decomposer will plan into child tasks.

        ``open_pr`` (default False for legacy behavior) is inherited by every
        child task the decomposer creates — under a standing goal with
        ``open_pr: true`` on the Action, each child task delivers as a
        reviewable-slice PR instead of committing directly to the workspace
        branch. ``verify_cmd`` (default None) is inherited the same way as the
        gate command. Closes the closeloop-mission-v2 defect where the
        activity-timeline program pushed straight to main because the flags
        stopped at ``submit_program`` and never reached child ``create_task``
        calls.

        ``pump=False`` (PR7 — see :meth:`submit`): create the program row
        ONLY — no planner kickoff (no ``_planning`` bookkeeping, no
        ``_plan_and_start`` spawn). The row lands 'planning' with zero
        tasks, which the EXISTING reconcile-from-DB-state logic in
        :meth:`_pump` already treats as "the planner never started (or
        died before persisting) — kick it off" — the same recovery path a
        crash mid-plan takes, reused here on purpose rather than duplicated.
        """
        program_id = str(uuid.uuid4())
        self._store.create_program(
            id=program_id, goal=goal, workspace_dir=workspace_dir,
            notify_url=notify_url, open_pr=open_pr, verify_cmd=verify_cmd,
            parent_goal_id=parent_goal_id,
        )
        if pump:
            self._planning.add(program_id)
            self._spawn(self._plan_and_start(program_id, workspace_dir, goal))
        return program_id

    def pump(self) -> None:
        """Public wrapper over the reconcile-from-DB-state core (PR7's
        dispatch/pump split). Callers that submitted with ``pump=False``
        invoke this AFTER their own atomic unit commits — e.g.
        ``InProcessEngine.kick()``, called by the goal heartbeat right after
        its dispatch transaction commits. Idempotent + cheap-idle-guarded,
        same as every other ``_pump()`` call site."""
        self._pump()

    # ---- crash recovery + heartbeat -------------------------------------

    def recover(self) -> int:
        """One-time crash recovery — call at startup, BEFORE ticking/serving.

        A task left ``running`` by a dead process has no live execution behind
        it, so reset it to ``pending`` to be re-run; log each reap. Programs left
        non-terminal are picked up by the next pump (re-planned if they never got
        tasks). Returns the number of tasks reaped.

        Also reaps the dead process's leaked sandbox CONTAINERS: the row reset
        below re-runs the task in a new container, but the original keeps
        running (``--rm`` only fires when its own docker client exits) with
        nothing left to stop it. recover() runs before this process launches
        anything, so every ``devclaw.sandbox``-labeled container is by
        definition orphaned. No-op when docker is unavailable (stub/host engine
        environments, CI).
        """
        swept = sweep_orphan_sandboxes()
        if swept:
            sys.stderr.write(
                f"task-queue: reaped {swept} orphaned sandbox container(s)\n"
            )
        reaped = self._store.reset_running_to_pending()
        for tid in reaped:
            t = self._store.get_task(tid)
            self._store.append_event(
                task_id=tid,
                program_id=t.program_id if t else None,
                type="reaped",
                source="devclaw",
                payload_json=json.dumps(
                    {"reason": "orphaned running task reset to pending on startup"}
                ),
            )
        if reaped:
            sys.stderr.write(f"task-queue: recovered {len(reaped)} orphaned running task(s)\n")
        return len(reaped)

    def start_ticking(self) -> None:
        """Start the heartbeat. Pumps immediately (resumes recovered work), then
        every TICK_SECONDS. Idempotent."""
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.ensure_future(self._tick_loop())

    async def stop_ticking(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

    async def _tick_loop(self) -> None:
        while True:
            try:
                self._pump()
            except Exception as err:  # a bad tick must never kill the heartbeat
                sys.stderr.write(f"task-queue: tick pump failed: {err}\n")
            await asyncio.sleep(TICK_SECONDS)

    # ---- the reconcile core ---------------------------------------------

    def _pump(self) -> None:
        """Reconcile execution against DB state: launch what's runnable up to the
        global + per-program caps, and terminalize finished programs. Synchronous
        and atomic (no awaits between reading counts and claiming), so concurrent
        callers can't over-launch; ``claim_pending`` is the final guard. Returns
        fast when there's no work (cheap-idle guard)."""
        # Global quota pause: a usage/rate limit is account-wide, so hold ALL
        # dispatch until it lifts. The tick loop calls _pump every TICK_SECONDS,
        # so dispatch auto-resumes within one tick of the pause expiring.
        until, reason = self._store.global_pause()
        if until:
            if _now_ms() < until:
                return
            self._store.clear_global_pause()
            sys.stderr.write(f"task-queue: quota pause expired ({reason[:80]}) — resuming\n")
        # Operator controls (manual pause toggle / daily run-window): hold ALL new
        # launches while active. In-flight tasks run to completion; the tick loop
        # re-checks every TICK_SECONDS, so dispatch resumes when the window opens.
        blocked, _why = operator_block(
            self._store.operator_hold(), self._store.get_run_schedule(), _now_ms()
        )
        if blocked:
            return
        if not self._store.has_active_work():
            return
        running = self._store.count_running()

        # Standalone pending tasks (no deps) — oldest first, up to the global cap.
        # Workspace circuit-breaker skips dispatch to a workspace whose recent
        # failure run tripped a hold; siblings on other workspaces keep flowing.
        if running < GLOBAL_MAX_CONCURRENT:
            for t in self._store.list_pending_standalone(limit=GLOBAL_MAX_CONCURRENT):
                if running >= GLOBAL_MAX_CONCURRENT:
                    break
                if self._workspace_break_active(t.workspace_dir):
                    continue
                if self._store.claim_pending(t.id):
                    running += 1
                    self._launch(t.id, t.kind, t.workspace_dir, t.goal, None)

        # Programs: re-plan stalled ones, terminalize complete ones, schedule ready.
        for p in self._store.list_nonterminal_programs():
            tasks = self._store.list_program_tasks(p.id)
            if p.status == "planning" and not tasks:
                if p.id not in self._planning:  # planner died before persisting → re-plan
                    self._planning.add(p.id)
                    self._spawn(self._plan_and_start(p.id, p.workspace_dir, p.goal))
                continue
            if self._maybe_terminalize(p, tasks):
                continue
            if self._workspace_break_active(p.workspace_dir):
                continue  # break holds new launches; in-flight tasks run to completion
            running = self._schedule_program(p, tasks, running)

    def _maybe_terminalize(self, program: Program, tasks: list[Task]) -> bool:
        """Mark the program done/failed (+ notify) if it has reached a terminal
        state. Returns True if it did."""
        all_done = len(tasks) > 0 and all(t.status == "done" for t in tasks)
        any_failed = any(t.status == "failed" for t in tasks)
        any_cancelled = any(t.status == "cancelled" for t in tasks)
        running_in_prog = sum(1 for t in tasks if t.status == "running")
        if all_done:
            self._store.mark_program_done(program.id)
            final = self._store.get_program(program.id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            self._fire_settle()  # a program terminalized → wake the goal layer
            return True
        # Sticky terminal: a failed or cancelled child blocks its dependents, so
        # the program can't complete. Terminalize once no sibling is still in
        # flight. Failure outranks cancellation (an error is worth surfacing).
        if (any_failed or any_cancelled) and running_in_prog == 0:
            if any_failed:
                first_err = (
                    next((t.error for t in tasks if t.status == "failed"), None)
                    or "task failed"
                )
                self._store.mark_program_failed(program.id, first_err)
            else:
                # Sweep still-pending siblings to cancelled too, so they don't
                # dangle 'pending' under a terminal program (which would keep
                # has_active_work() true forever). The failure path above leaves
                # them as-is — unchanged behavior, deliberately.
                self._store.cancel_program_pending_tasks(program.id)
                self._store.mark_program_cancelled(
                    program.id, error="a task was cancelled"
                )
            final = self._store.get_program(program.id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            self._fire_settle()  # program failed/cancelled → wake the goal layer
            return True
        return False

    def _schedule_program(self, program: Program, tasks: list[Task], running: int) -> int:
        """Launch a program's ready tasks (deps all done) up to both caps. A
        present failure OR cancellation suppresses new launches (sticky) — the
        program is about to terminalize. Returns the updated global running tally."""
        if any(t.status in ("failed", "cancelled") for t in tasks):
            return running
        by_id = {t.id: t for t in tasks}
        running_in_prog = sum(1 for t in tasks if t.status == "running")
        for t in tasks:
            if running >= GLOBAL_MAX_CONCURRENT or running_in_prog >= MAX_CONCURRENT_PER_PROGRAM:
                break
            if t.status != "pending":
                continue
            deps_ready = all(
                (by_id.get(d) is not None and by_id[d].status == "done") for d in t.depends_on
            )
            if not deps_ready:
                continue
            if not self._store.claim_pending(t.id):  # lost the race
                continue
            running += 1
            running_in_prog += 1
            self._launch(t.id, t.kind, t.workspace_dir, t.goal, program.id)
        return running

    def _launch(
        self,
        task_id: str,
        kind: TaskKind,
        workspace_dir: str,
        goal: str,
        program_id: Optional[str],
    ) -> None:
        task = self._spawn(self._execute(task_id, kind, workspace_dir, goal, program_id))
        # Index it for cancel(); drop the ref the moment it settles so the map
        # only ever names genuinely in-flight runs.
        self._running_tasks[task_id] = task
        task.add_done_callback(lambda _t, tid=task_id: self._running_tasks.pop(tid, None))

    async def _execute(
        self,
        task_id: str,
        kind: TaskKind,
        workspace_dir: str,
        goal: str,
        program_id: Optional[str],
    ) -> None:
        # The task is already 'running' (claim_pending set it); just run + settle.
        # An open_pr task (standalone OR program-child) must NOT be observable as
        # 'done' until its change is delivered — otherwise a poller (goalclaw)
        # reads done-without-PR the instant the gate passes and re-dispatches
        # an already-shipped item. So for that path we defer the done-flip:
        # run delivery while the task is still 'running', then settle 'done'
        # WITH the pr_url in one write.
        #
        # Program tasks inherit their ``deliver`` flag from the parent program's
        # ``open_pr`` (set at submit_program time; see ``_persist_plan``) — the
        # standing-goal reviewable-slice contract, closing the 2026-07-03
        # closeloop-mission-v2 defect where the activity-timeline program
        # pushed straight to main.
        row = self._store.get_task(task_id)
        deliver = bool(row and row.deliver)
        success = await self._run_and_settle(
            task_id, kind, workspace_dir, goal, defer_done=deliver
        )
        if success is _PAUSED:
            # Paused for a quota limit — task is back to 'pending', global pause
            # holds dispatch. Don't deliver/notify/settle; the gated _pump will
            # redispatch it (fresh attempts) once the pause expires.
            self._pump()
            return
        if deliver and success is not None:
            # Gate passed; the task is still 'running'. Turn the change into a
            # branch/PR, then make 'done' observable — with pr_url already on
            # the row. Pass the kind (→ conventional-commit title) + the gate
            # verdict (→ PR body) so the delivered PR describes itself.
            verify = success.get("verify") if isinstance(success, dict) else None
            pr_url = None
            failure: Optional[str] = None
            delivery: dict = {}
            try:
                delivery = await deliver_change(
                    workspace_dir=workspace_dir, task_id=task_id, goal=goal,
                    kind=kind, verify=verify,
                    title=(row.title if row else None),
                )
                pr_url = delivery.get("pr_url")
                failure = delivery_failed(delivery)
                sys.stderr.write(f"task-queue: delivery task={task_id}: {delivery}\n")
            except Exception as err:  # deliver_change promises not to raise; belt+suspenders
                failure = f"{err.__class__.__name__}: {err}"
                sys.stderr.write(f"task-queue: delivery failed task={task_id}: {err}\n")
            if isinstance(success, dict):
                # The delivery verdict is grounded evidence — persist it with the
                # result so the goal poller reads the PR/branch/push state, not
                # just a bare pr_url column.
                success["delivery"] = delivery
            if failure is not None and not pr_url:
                # A requested delivery that BROKE must not settle 'done': a
                # done-without-PR row reads as shipped to every poller upstream
                # (the goal layer plans its next action off it — the exact
                # false-green the defer_done mechanism exists to prevent).
                # Benign no-PR outcomes (nothing to ship, local-only repo) are
                # not failures — delivery_failed() filters those out above.
                self._store.mark_failed(
                    task_id, f"gate passed but delivery failed: {failure}"
                )
                self._check_and_trip_breaker(workspace_dir, task_id)
            else:
                # 'done' becomes observable only now, atomically with pr_url.
                self._store.mark_done(task_id, json.dumps(success), pr_url=pr_url)
        if program_id is None:
            final = self._store.get_task(task_id)
            if final and final.notify_url:
                await self._notify_task(final)
            self._fire_settle()  # a standalone task settled → wake the goal layer
        # A global slot freed; this program may now be complete or have newly-ready
        # tasks; another program may be able to start. Re-pump.
        self._pump()

    def _persist_plan(
        self, program_id: str, workspace_dir: str, planned: list[PlannedTask]
    ) -> None:
        """Map planner keys -> real UUIDs and insert the program's tasks with
        depends_on remapped. Runs as one batch before anything is scheduled, so
        the dep graph is fully consistent by the time the first task starts.

        Child tasks INHERIT the program's ``open_pr`` and ``verify_cmd`` — the
        standing-goal / reviewable-slice contract. Review tasks
        (``review_repository``) always skip PR + gate because they write a
        read-only report, matching the standalone-task rule at engine.py."""
        program = self._store.get_program(program_id)
        # Legacy programs (created before the 2026-07-03 column addition) load
        # with open_pr=False / verify_cmd=None; that preserves the pre-change
        # behavior for any in-flight program at deploy time.
        program_open_pr = bool(program and program.open_pr)
        program_verify_cmd = program.verify_cmd if program else None
        key_to_uuid = {p.key: str(uuid.uuid4()) for p in planned}
        for idx, p in enumerate(planned):
            dep_uuids: list[str] = []
            for k in p.depends_on_keys:
                u = key_to_uuid.get(k)
                if not u:  # should never happen — validate_plan rejects dangling refs
                    raise RuntimeError(f"planner produced dangling ref '{k}'")
                dep_uuids.append(u)
            is_review = p.kind == "review_repository"
            self._store.create_task(
                id=key_to_uuid[p.key],
                kind=p.kind,
                workspace_dir=workspace_dir,
                goal=p.goal,
                notify_url=None,  # per-task notify omitted — only program-level fires
                program_id=program_id,
                depends_on=dep_uuids,
                order_idx=idx,
                milestone=p.milestone,
                verify_cmd=None if is_review else program_verify_cmd,
                deliver=False if is_review else program_open_pr,
            )

    def start_planned_program(
        self,
        *,
        goal: str,
        workspace_dir: str,
        planned: list[PlannedTask],
        notify_url: Optional[str] = None,
    ) -> str:
        """Submit an ALREADY-PLANNED program (caller did the planning, e.g.
        plan_spec for an approved project). Persists the DAG and starts it
        synchronously — never observed in 'planning', so no plan-time recovery
        edge case. Returns the program_id."""
        program_id = str(uuid.uuid4())
        self._store.create_program(
            id=program_id, goal=goal, workspace_dir=workspace_dir, notify_url=notify_url
        )
        self._persist_plan(program_id, workspace_dir, planned)
        self._store.mark_program_running(program_id)
        self._pump()
        return program_id

    async def _plan_and_start(self, program_id: str, workspace_dir: str, goal: str) -> None:
        try:
            try:
                planned = await self._planner(goal, workspace_dir)
            except Exception as err:
                msg = f"planner: {err}" if isinstance(err, PlannerError) else str(err)
                self._store.mark_program_failed(program_id, msg)
                program = self._store.get_program(program_id)
                if program:
                    await self._notify_program(program, [])
                return
            self._persist_plan(program_id, workspace_dir, planned)
            self._store.mark_program_running(program_id)
        finally:
            self._planning.discard(program_id)
        self._pump()

    # ---- shared runner --------------------------------------------------

    async def _run_and_settle(
        self, task_id: str, kind: TaskKind, workspace_dir: str, goal: str,
        *, defer_done: bool = False,
    ) -> Optional[dict]:
        """Run the agent (with retries) and settle the task. Returns None once the
        task is settled (done/failed/timeout). When ``defer_done`` is set and the
        gate passes, it does NOT mark the task done — it returns the winning result
        dict and leaves the task 'running', so the caller can deliver then settle
        'done' atomically (see _execute). Failures/timeouts always settle here."""
        # Resolve program_id + the verify gate once so on_event doesn't re-query.
        row = self._store.get_task(task_id)
        program_id = row.program_id if row else None
        verify_cmd = row.verify_cmd if row else None
        # L3 (#222): a scaffold task skips ONLY the adversarial review gate below.
        # The verify gate (checked first) and test-integrity scan are NOT gated on
        # this flag — they run for scaffold and non-scaffold tasks alike, so an
        # over-tagged real code task still fails if it doesn't build or guts tests.
        scaffold = bool(row.scaffold) if row else False

        # Resumed-after-interruption brief. ``pause_count > 0`` means a previous
        # attempt of THIS task was cut off by a usage limit and requeued — the
        # T0.6 counter is the durable interruption signal, so no schema change
        # is needed here. The workspace survives the requeue untouched (nothing
        # re-preps between requeue and re-run), so its partial progress is still
        # there — possibly as a wip snapshot commit — and a re-run handed the
        # pristine goal would restart from scratch or duplicate/conflict with
        # the half-done edits. The brief is prepended to EVERY attempt this run
        # makes (a resumed task can also retry: brief prefix + goal + retry
        # suffix compose), and stays distinct from the retry-feedback suffix.
        pause_count = row.pause_count if row else 0
        resume_brief = "" if pause_count <= 0 else (
            f"[Resuming after a usage-limit interruption (pause {pause_count})] "
            "A previous attempt was cut off mid-work. The workspace already "
            "contains its partial progress — possibly including a "
            "'wip(devclaw): interrupted…' commit. Inspect `git status` and "
            "`git log` first and CONTINUE from where it left off; do not "
            "restart or redo completed work.\n\n"
        )

        def on_event(event: EngineEvent) -> None:
            try:
                self._store.append_event(
                    task_id=task_id,
                    program_id=program_id,
                    type=event.type,
                    source=event.source,
                    payload_json=json.dumps(event.payload),
                    ts=int(event.ts) if isinstance(event.ts, (int, float)) else _now_ms(),
                )
            except Exception as err:  # event writes must never crash the run
                sys.stderr.write(f"task-queue: append_event failed task={task_id}: {err}\n")

        # Baseline for the post-gate diff. The agent is asked to COMMIT its work
        # (goal-branch mode lands commits directly on goal/<id>), so the tree can
        # be clean by settle time — the gates must diff against the pre-run HEAD
        # to see the change at all. Captured ONCE before the attempt loop, not
        # per attempt: delivery ships everything ahead of this ref, so a retry's
        # gates must judge the same cumulative span it will ship.
        pre_run_sha = await _git_head(workspace_dir)

        # Retry-on-fail completes the reliability triad (verify + RETRY + human): a
        # gate-fail or a transient agent error is re-run, each time with the failure
        # fed back into the goal so the agent can self-correct, up to a bounded cap;
        # then it's escalated (the notify fires on the terminal state). Timeouts are
        # NOT retried — a stuck run would likely just hang again — they escalate now.
        attempts = 1 + max(0, TASK_MAX_RETRIES)
        last_failure = "unknown error"
        for attempt in range(attempts):
            attempt_goal = f"{resume_brief}{goal}" if attempt == 0 else (
                f"{resume_brief}{goal}\n\n[Automatic retry {attempt}/{attempts - 1}] Your previous "
                f"attempt did not pass verification. What went wrong:\n{last_failure}\n\n"
                f"Diagnose the cause and fix it; do not repeat the same mistake."
            )
            request = EngineRequest(
                kind=kind,
                workspace_dir=workspace_dir,
                goal=attempt_goal,
                on_event=on_event,
                verify_cmd=verify_cmd,
            )
            try:
                # Wall-clock guard: on timeout, wait_for cancels the runner coroutine,
                # which propagates into run_sandcastle's finally → docker rm -f, so the
                # sandbox is torn down. Same cancellation path explicit cancel_task uses
                # (CancelledError is not an Exception, so a real cancel still propagates).
                if TASK_TIMEOUT_S > 0:
                    result = await asyncio.wait_for(self._runner(request), timeout=TASK_TIMEOUT_S)
                else:
                    result = await self._runner(request)
            except asyncio.TimeoutError:
                self._store.mark_failed(
                    task_id,
                    f"task exceeded the {TASK_TIMEOUT_S:.0f}s wall-clock timeout with no "
                    f"terminal result — sandbox torn down. Raise DEVCLAW_TASK_TIMEOUT_S "
                    f"if this was a legitimately long task.",
                )
                self._check_and_trip_breaker(workspace_dir, task_id)
                return None
            except Exception as err:
                last_failure = str(err)  # unexpected runner error — retryable
            else:
                if result.get("status") != "ok":
                    last_failure = result.get("error", "unknown error")
                    if result.get("status") == "rate_limited" and result.get("retry_after"):
                        # the engineer parsed an explicit reset hint — prefer it
                        last_failure = f"rate limit; retry-after: {result['retry_after']}s"
                else:
                    # "done" means the verify gate passed, not that the agent said so.
                    verify = result.get("verify")
                    if verify and verify.get("ran") and not verify.get("passed"):
                        last_failure = _verify_failure_summary(verify)
                    else:
                        # Gate passed — now the checks that READ the change. Compute
                        # the diff once and share it between the test-integrity guard
                        # and the adversarial review gate.
                        # NB: git runs in THIS (host/server) process, so it needs the
                        # workspace path as we see it — NOT the docker-bind host path.
                        # `_translate_workspace_path` maps container→host for the
                        # sandbox `-v` mount; using it here pointed git at a path that
                        # doesn't exist in our mount namespace (`/srv/...`), so the
                        # diff came back empty and BOTH guards silently no-op'd in
                        # the deployed container. Use workspace_dir directly.
                        diff = await _git_diff(workspace_dir, pre_run_sha)
                        integrity = _integrity_failure(diff)
                        review_fb = (
                            None if integrity is not None
                            else await self._review_failure(
                                kind, goal, diff, workspace_dir, scaffold=scaffold
                            )
                        )
                        if integrity is not None:
                            # gate passed but the change weakened the tests — treat as
                            # a gate failure so it retries with the tampering fed back.
                            last_failure = integrity
                        elif review_fb is not None:
                            # gate + tests fine, but review found a real defect — feed
                            # the issues back through the SAME retry loop as a gate fail.
                            last_failure = review_fb
                        elif defer_done:
                            # caller delivers, then settles 'done' WITH pr_url atomically
                            return result
                        else:
                            self._store.mark_done(task_id, json.dumps(result))
                            return None
            # Quota guard: a usage/rate limit must NOT be retried-now (that burns
            # the remaining quota on the same doomed call). Pause ALL dispatch and
            # requeue this task; the tick loop auto-resumes when the pause expires.
            # now_utc lets the classifier turn Claude's ABSOLUTE reset wording
            # ("resets 10pm (UTC)") into a real hint; a stated hint is trusted
            # past the default re-probe cap (pause_seconds' stated policy).
            cls = classify_failure(last_failure, now_utc=datetime.now(timezone.utc))
            if cls.is_pausing:
                backoff = pause_seconds(cls.retry_after_s, stated=cls.stated)
                self._store.set_global_pause(
                    _now_ms() + backoff * 1000, f"{cls.kind.value}: {last_failure[:160]}"
                )
                task = self._store.get_task(task_id)
                if task is not None and task.pause_count >= MAX_PAUSE_REQUEUES:
                    # This one task has ridden the pause loop to its bound — fail
                    # it with the real reason so the breaker (and a human) can
                    # see it, instead of requeueing forever. The global pause
                    # above still holds: the account IS limited.
                    self._store.mark_failed(
                        task_id,
                        f"exceeded {MAX_PAUSE_REQUEUES} usage-limit pauses; "
                        f"last: {last_failure}",
                    )
                    self._check_and_trip_breaker(workspace_dir, task_id)
                    sys.stderr.write(
                        f"task-queue: task {task_id} hit {cls.kind.value} after "
                        f"{task.pause_count} pause-requeues — failing (bound "
                        f"{MAX_PAUSE_REQUEUES} reached), dispatch still paused "
                        f"~{backoff}s\n"
                    )
                    return None
                # Preserve the interrupted attempt's partial work BEFORE the
                # requeue: commit the dirty tree as a wip snapshot so it can't
                # be wiped by a later workspace reset/clean. Best-effort — any
                # snapshot failure logs and the pause path proceeds regardless.
                try:
                    snapshot = await _wip_snapshot(workspace_dir, task_id)
                except Exception as err:  # noqa: BLE001 — never block the pause
                    snapshot = f"crashed: {err.__class__.__name__}: {err}"
                if snapshot == "committed":
                    sys.stderr.write(
                        f"task-queue: task {task_id} wip snapshot committed "
                        f"before pause requeue\n"
                    )
                else:
                    sys.stderr.write(
                        f"task-queue: task {task_id} wip snapshot skipped "
                        f"({snapshot})\n"
                    )
                self._store.requeue_task(task_id)
                sys.stderr.write(
                    f"task-queue: task {task_id} hit {cls.kind.value} — pausing dispatch "
                    f"~{backoff}s, requeued (not failed)\n"
                )
                return _PAUSED
            # A review-gate CRASH (the reviewer couldn't produce a verdict — an
            # oversized/unparseable diff makes the review model return non-JSON) is
            # NOT a defect the agent can fix by retrying: re-running produces the
            # same diff and re-crashes the gate identically, burning the retry budget
            # and then the goal-level re-dispatch loop. Fail FAST + fail CLOSED (never
            # ship unreviewed) with an actionable reason, instead of looping. Quota-
            # shaped reviewer crashes are handled above (they PAUSE, not fail).
            if last_failure.startswith(_REVIEW_CRASH_MARKER):
                self._store.mark_failed(
                    task_id,
                    f"{last_failure} Not auto-retried: a diff too large or "
                    "unreviewable for the gate must be split into smaller commits or "
                    "reviewed by a human — retrying re-crashes the gate identically.",
                )
                self._check_and_trip_breaker(workspace_dir, task_id)
                return None
            if attempt < attempts - 1:
                sys.stderr.write(
                    f"task-queue: task {task_id} attempt {attempt + 1}/{attempts} failed; "
                    f"retrying with the failure fed back\n"
                )
        # every attempt failed — escalate.
        suffix = f" (failed after {attempts} attempts)" if attempts > 1 else ""
        self._store.mark_failed(task_id, f"{last_failure}{suffix}")
        self._check_and_trip_breaker(workspace_dir, task_id)
        return None

    def _review_gate_enabled(self, workspace_dir: str) -> bool:
        """Whether the pre-PR review gate runs for a task in ``workspace_dir``:
        the owning project's ``review_gate`` override if set, else the
        devclaw-wide ``REVIEW_GATE_ENABLED`` default. No registry wired → the
        default."""
        if self._registry is None:
            return REVIEW_GATE_ENABLED
        return self._registry.resolve_override(
            workspace_dir, "review_gate", REVIEW_GATE_ENABLED
        )

    async def _review_failure(
        self, kind: TaskKind, goal: str, diff: str, workspace_dir: str,
        *, scaffold: bool = False,
    ) -> Optional[str]:
        """Run the pre-PR adversarial review gate on the change's diff. Returns the
        request-changes feedback (→ fed back into the retry loop like a gate fail),
        or None to let the task ship. Fails open ONLY for the by-design cases —
        a disabled gate, a non-code kind, a SCAFFOLD task, an empty diff. A
        reviewer CRASH fails CLOSED: a crash is not an approval, and the old
        failing-open path meant any internal reviewer error shipped the change
        unreviewed with a line on stderr nobody reads. A quota/rate-limit crash
        text is classified by the caller's quota guard and PAUSES (requeue +
        resume) instead of failing — the correct semantics for "the reviewer
        couldn't run right now".

        SCAFFOLD (L3, #222): a generated-scaffolding task (``ng new`` /
        ``dotnet new`` boilerplate) skips this ADVERSARIAL gate — its diff is
        generator output, not hand-authored logic, and an oversized generated
        diff crashes the review model. This is safe because it's a NARROW bypass:
        the caller has ALREADY passed the change through the verify/build gate and
        the test-integrity scan before reaching here, and neither of those is
        gated on ``scaffold``. So even a MIS-tagged real code task is at worst
        "unreviewed but still must build + pass tests", never "ships broken"."""
        if not self._review_gate_enabled(workspace_dir) or kind not in _REVIEWABLE_KINDS:
            return None
        if scaffold:
            sys.stderr.write(
                "task-queue: scaffold task — skipping adversarial review gate "
                "(verify gate + test-integrity already enforced)\n"
            )
            return None
        if not diff.strip():
            return None
        try:
            review = await self._reviewer(goal=goal, kind=kind, diff=diff)
        except Exception as err:  # noqa: BLE001 — fail closed, never silently approve
            sys.stderr.write(f"task-queue: {_REVIEW_CRASH_MARKER} {err}\n")
            return (
                f"{_REVIEW_CRASH_MARKER} "
                f"{err.__class__.__name__}: {err}. The diff was never reviewed, "
                "so it must not ship on the gate's silence."
            )
        if review.get("verdict") == "request_changes":
            sys.stderr.write(
                f"task-queue: review gate requested changes "
                f"({len(review.get('blocking', []))} blocking issue(s))\n"
            )
            return format_feedback(review)
        return None

    # ---- notify ---------------------------------------------------------
    # _notify_task / _notify_program / _post_with_retries live in
    # devclaw.task_notify._NotifyMixin (mixed into this class above).
