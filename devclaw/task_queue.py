"""In-process async task executor.

The MCP handler calls ``submit()`` or ``submit_program()`` and gets an id back
immediately; the OpenHands subprocesses run in the background (asyncio tasks)
and the state store flips rows when they settle. Single-writer-to-state by
design — only this queue mutates task rows.

Programs (DAGs):
  ``submit_program()`` returns a program_id synchronously and then, in the
  background, (1) runs the planner, (2) on success persists tasks with
  depends_on remapped to UUIDs and schedules tasks whose deps are satisfied,
  (3) on planner failure marks the program failed and fires its notify. When a
  child settles, the queue schedules newly-ready siblings, decrements the
  in-flight counter, and once nothing is in flight and the program is terminal
  fires the program-level notify.

Failure policy: a single sibling failure makes the program "sticky failed" —
pending siblings won't start, in-flight ones run to completion.

Notifications:
  - Standalone tasks fire their own notify_url on terminal state, bounded
    retries (1s/2s/4s).
  - Program-child tasks do NOT fire per-task callbacks — only the program-level
    notify fires once the program terminates (one program in, one notify out).
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
        #: in-flight counter per program, enforces the concurrency cap
        self._running_by_program: dict[str, int] = {}
        #: retain background task refs so they aren't garbage-collected mid-run
        self._bg: set[asyncio.Task] = set()

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

    # ---- standalone task path -------------------------------------------

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
        self._spawn(self._execute_standalone(task_id, kind, workspace_dir, goal))
        return task_id

    async def _execute_standalone(
        self, task_id: str, kind: TaskKind, workspace_dir: str, goal: str
    ) -> None:
        self._store.mark_running(task_id)
        await self._run_and_settle(task_id, kind, workspace_dir, goal)
        final = self._store.get_task(task_id)
        if final and final.notify_url:
            await self._notify_task(final)

    # ---- program path ---------------------------------------------------

    def submit_program(
        self, *, workspace_dir: str, goal: str, notify_url: Optional[str] = None
    ) -> str:
        program_id = str(uuid.uuid4())
        self._store.create_program(
            id=program_id, goal=goal, workspace_dir=workspace_dir, notify_url=notify_url
        )
        self._spawn(self._plan_and_start(program_id, workspace_dir, goal))
        return program_id

    async def _plan_and_start(self, program_id: str, workspace_dir: str, goal: str) -> None:
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
        self._schedule_ready(program_id)

    def _schedule_ready(self, program_id: str) -> None:
        """Find pending tasks whose deps are all done and launch up to the
        concurrency cap. Safe to call repeatedly — claim_pending is atomic. If
        the program has already failed (sticky), launch nothing."""
        program = self._store.get_program(program_id)
        if not program or program.status in ("failed", "done"):
            return
        tasks = self._store.list_program_tasks(program_id)
        by_id = {t.id: t for t in tasks}
        # If any task already failed, suppress new launches. The program's
        # transition to 'failed' happens in _advance_program once in-flight = 0.
        if any(t.status == "failed" for t in tasks):
            return
        in_flight = self._running_by_program.get(program_id, 0)
        for t in tasks:
            if in_flight >= MAX_CONCURRENT_PER_PROGRAM:
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
            in_flight += 1
            self._running_by_program[program_id] = in_flight
            self._spawn(
                self._execute_program_task(program_id, t.id, t.kind, t.workspace_dir, t.goal)
            )

    async def _execute_program_task(
        self, program_id: str, task_id: str, kind: TaskKind, workspace_dir: str, goal: str
    ) -> None:
        await self._run_and_settle(task_id, kind, workspace_dir, goal)
        # Decrement in-flight, then re-evaluate scheduling + termination.
        n = self._running_by_program.get(program_id, 1) - 1
        if n <= 0:
            self._running_by_program.pop(program_id, None)
        else:
            self._running_by_program[program_id] = n
        self._advance_program(program_id)

    def _advance_program(self, program_id: str) -> None:
        """Evaluate program state after a child terminated: all done -> done +
        notify; any failed and nothing in flight -> failed + notify (sticky);
        otherwise schedule newly-ready tasks."""
        program = self._store.get_program(program_id)
        if not program or program.status in ("done", "failed"):
            return  # already terminal — notify already fired (or will, in another path)
        tasks = self._store.list_program_tasks(program_id)
        all_done = len(tasks) > 0 and all(t.status == "done" for t in tasks)
        any_failed = any(t.status == "failed" for t in tasks)
        in_flight = self._running_by_program.get(program_id, 0)

        if all_done:
            self._store.mark_program_done(program_id)
            final = self._store.get_program(program_id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            return
        if any_failed and in_flight == 0:
            first_err = next((t.error for t in tasks if t.status == "failed"), None) or "task failed"
            self._store.mark_program_failed(program_id, first_err)
            final = self._store.get_program(program_id)
            if final:
                self._spawn(self._notify_program(final, tasks))
            return
        if not any_failed:
            self._schedule_ready(program_id)

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
