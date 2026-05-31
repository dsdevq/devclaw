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
from typing import Awaitable, Callable, Optional

import httpx

from .engine import Engine, EngineEvent, EngineRequest
from .planner import PlannedTask, PlannerError, plan_goal
from .sandcastle_runner import run_sandcastle
from .state_store import Program, StateStore, Task, TaskKind, _now_ms

NOTIFY_BACKOFF_MS = (1000, 2000, 4000)
MAX_CONCURRENT_PER_PROGRAM = int(os.environ.get("DEVCLAW_MAX_CONCURRENT_PER_PROGRAM", "2"))
#: global cap on concurrently-running tasks across all programs — backpressure
GLOBAL_MAX_CONCURRENT = int(os.environ.get("DEVCLAW_MAX_CONCURRENT", "4"))
#: heartbeat interval — the tick re-derives scheduling from DB state
TICK_SECONDS = float(os.environ.get("DEVCLAW_TICK_SECONDS", "10"))

PlannerFn = Callable[[str, str], Awaitable[list[PlannedTask]]]
#: the execution engine — orchestration depends on this seam, not on OpenHands
RunnerFn = Engine


class TaskQueue:
    def __init__(
        self,
        store: StateStore,
        planner: Optional[PlannerFn] = None,
        runner: Optional[RunnerFn] = None,
    ) -> None:
        self._store = store
        # Injectable for tests — default to the real planner / sandcastle runner.
        self._planner: PlannerFn = planner or (lambda g, w: plan_goal(g, w))
        self._runner: RunnerFn = runner or run_sandcastle
        #: retain background task refs so they aren't garbage-collected mid-run
        self._bg: set[asyncio.Task] = set()
        #: program_ids whose planner is in flight — guards against double-plan
        self._planning: set[str] = set()
        #: the heartbeat tick task (started by the server, not in tests)
        self._tick_task: Optional[asyncio.Task] = None

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

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
    ) -> str:
        task_id = str(uuid.uuid4())
        self._store.create_task(
            id=task_id, kind=kind, workspace_dir=workspace_dir, goal=goal, notify_url=notify_url
        )
        self._pump()
        return task_id

    def submit_program(
        self, *, workspace_dir: str, goal: str, notify_url: Optional[str] = None
    ) -> str:
        program_id = str(uuid.uuid4())
        self._store.create_program(
            id=program_id, goal=goal, workspace_dir=workspace_dir, notify_url=notify_url
        )
        self._planning.add(program_id)
        self._spawn(self._plan_and_start(program_id, workspace_dir, goal))
        return program_id

    # ---- crash recovery + heartbeat -------------------------------------

    def recover(self) -> int:
        """One-time crash recovery — call at startup, BEFORE ticking/serving.

        A task left ``running`` by a dead process has no live execution behind
        it, so reset it to ``pending`` to be re-run; log each reap. Programs left
        non-terminal are picked up by the next pump (re-planned if they never got
        tasks). Returns the number of tasks reaped.
        """
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
        if not self._store.has_active_work():
            return
        running = self._store.count_running()

        # Standalone pending tasks (no deps) — oldest first, up to the global cap.
        if running < GLOBAL_MAX_CONCURRENT:
            for t in self._store.list_pending_standalone(limit=GLOBAL_MAX_CONCURRENT):
                if running >= GLOBAL_MAX_CONCURRENT:
                    break
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
            running = self._schedule_program(p, tasks, running)

    def _maybe_terminalize(self, program: Program, tasks: list[Task]) -> bool:
        """Mark the program done/failed (+ notify) if it has reached a terminal
        state. Returns True if it did."""
        all_done = len(tasks) > 0 and all(t.status == "done" for t in tasks)
        any_failed = any(t.status == "failed" for t in tasks)
        running_in_prog = sum(1 for t in tasks if t.status == "running")
        if all_done:
            self._store.mark_program_done(program.id)
            final = self._store.get_program(program.id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            return True
        # sticky failure: fail once no sibling is still running
        if any_failed and running_in_prog == 0:
            first_err = next((t.error for t in tasks if t.status == "failed"), None) or "task failed"
            self._store.mark_program_failed(program.id, first_err)
            final = self._store.get_program(program.id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            return True
        return False

    def _schedule_program(self, program: Program, tasks: list[Task], running: int) -> int:
        """Launch a program's ready tasks (deps all done) up to both caps. A
        present failure suppresses new launches (sticky). Returns the updated
        global running tally."""
        if any(t.status == "failed" for t in tasks):
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
        self._spawn(self._execute(task_id, kind, workspace_dir, goal, program_id))

    async def _execute(
        self,
        task_id: str,
        kind: TaskKind,
        workspace_dir: str,
        goal: str,
        program_id: Optional[str],
    ) -> None:
        # The task is already 'running' (claim_pending set it); just run + settle.
        await self._run_and_settle(task_id, kind, workspace_dir, goal)
        if program_id is None:
            final = self._store.get_task(task_id)
            if final and final.notify_url:
                await self._notify_task(final)
        # A global slot freed; this program may now be complete or have newly-ready
        # tasks; another program may be able to start. Re-pump.
        self._pump()

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

            # Map planner keys -> real UUIDs, then persist tasks with depends_on
            # remapped. The whole insert runs before any task is scheduled, so the
            # dep graph is fully consistent by the time the first task starts.
            key_to_uuid = {p.key: str(uuid.uuid4()) for p in planned}
            for idx, p in enumerate(planned):
                dep_uuids: list[str] = []
                for k in p.depends_on_keys:
                    u = key_to_uuid.get(k)
                    if not u:  # should never happen — validate_plan rejects dangling refs
                        raise RuntimeError(f"planner produced dangling ref '{k}'")
                    dep_uuids.append(u)
                self._store.create_task(
                    id=key_to_uuid[p.key],
                    kind=p.kind,
                    workspace_dir=workspace_dir,
                    goal=p.goal,
                    notify_url=None,  # per-task notify omitted — only program-level fires
                    program_id=program_id,
                    depends_on=dep_uuids,
                    order_idx=idx,
                )
            self._store.mark_program_running(program_id)
        finally:
            self._planning.discard(program_id)
        self._pump()

    # ---- shared runner --------------------------------------------------

    async def _run_and_settle(
        self, task_id: str, kind: TaskKind, workspace_dir: str, goal: str
    ) -> None:
        # Resolve program_id once so on_event doesn't re-query per event.
        row = self._store.get_task(task_id)
        program_id = row.program_id if row else None

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

        try:
            result = await self._runner(
                EngineRequest(kind=kind, workspace_dir=workspace_dir, goal=goal, on_event=on_event)
            )
            if result.get("status") == "ok":
                self._store.mark_done(task_id, json.dumps(result))
            else:
                self._store.mark_failed(task_id, result.get("error", "unknown error"))
        except Exception as err:
            self._store.mark_failed(task_id, str(err))

    # ---- notify ---------------------------------------------------------

    async def _notify_task(self, task: Task) -> None:
        if not task.notify_url:
            return
        payload = {
            "task_id": task.id,
            "status": task.status,
            "kind": task.kind,
            "workspace_dir": task.workspace_dir,
            "goal": task.goal,
            "result_json": task.result_json,
            "error": task.error,
            "terminated_at": task.completed_at,
        }
        await self._post_with_retries(task.notify_url, payload, f"task={task.id}")

    async def _notify_program(self, program: Program, tasks: list[Task]) -> None:
        if not program.notify_url:
            return
        payload = {
            "program_id": program.id,
            "status": program.status,
            "goal": program.goal,
            "workspace_dir": program.workspace_dir,
            "error": program.error,
            "terminated_at": program.completed_at,
            "tasks": [
                {
                    "task_id": t.id,
                    "kind": t.kind,
                    "status": t.status,
                    "goal": t.goal,
                    "depends_on": t.depends_on,
                    "result_json": t.result_json,
                    "error": t.error,
                }
                for t in tasks
            ],
        }
        await self._post_with_retries(program.notify_url, payload, f"program={program.id}")

    async def _post_with_retries(self, url: str, payload: dict, tag: str) -> None:
        async with httpx.AsyncClient() as client:
            for attempt in range(len(NOTIFY_BACKOFF_MS)):
                try:
                    res = await client.post(url, json=payload, timeout=10.0)
                    if res.is_success:
                        sys.stderr.write(
                            f"notify ok {tag} url={url} status={res.status_code} attempt={attempt + 1}\n"
                        )
                        return
                    sys.stderr.write(
                        f"notify non-2xx {tag} url={url} status={res.status_code} attempt={attempt + 1}\n"
                    )
                except Exception as err:
                    sys.stderr.write(
                        f'notify error {tag} url={url} err="{err}" attempt={attempt + 1}\n'
                    )
                if attempt < len(NOTIFY_BACKOFF_MS) - 1:
                    await asyncio.sleep(NOTIFY_BACKOFF_MS[attempt] / 1000)
        sys.stderr.write(
            f"notify WARN giving up {tag} url={url} after {len(NOTIFY_BACKOFF_MS)} attempts\n"
        )
