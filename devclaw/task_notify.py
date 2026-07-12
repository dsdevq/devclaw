"""Terminal-state notification for tasks and programs.

Split out of :mod:`devclaw.task_queue` as a mixin so the ~950-line single-writer
core stays focused. Standalone tasks fire their own ``notify_url`` on a terminal
state (bounded retries); program-child tasks don't — only the program-level
notify fires once the program terminates (one program in, one notify out).

``NOTIFY_BACKOFF_MS`` stays a module-level constant in :mod:`devclaw.task_queue`
(the queue's constants are pinned to that namespace by tests); the mixin reads it
through the ``task_queue`` module object so a monkeypatch there is honored.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from . import task_queue
from .state_store import Program, Task


class _NotifyMixin:
    """Notify behavior mixed into :class:`devclaw.task_queue.TaskQueue`."""

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
            "pr_url": task.pr_url,
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
        backoff_ms = task_queue.NOTIFY_BACKOFF_MS
        async with httpx.AsyncClient() as client:
            for attempt in range(len(backoff_ms)):
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
                if attempt < len(backoff_ms) - 1:
                    await asyncio.sleep(backoff_ms[attempt] / 1000)
        sys.stderr.write(
            f"notify WARN giving up {tag} url={url} after {len(backoff_ms)} attempts\n"
        )
