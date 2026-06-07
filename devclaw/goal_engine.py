"""The engine seam — in-process, replacing goalclaw's HTTP MCP client.

When the goal layer lived in a separate service it dispatched to devclaw over
streamable-http MCP and polled task rows back as a camelCase JSON blob. Folded
in, the seam collapses to a direct call into :class:`TaskQueue` /
:class:`StateStore`: ``dispatch`` submits a task/program and ``poll`` reads the
row straight from SQLite. This deletes the HTTP transport, the bearer token, the
``/wake`` endpoint, AND the whole "polled `done` before `pr_url` was written"
timing race — in-process there is no over-the-wire window.

It also reads RICHER signal than the wire ever exposed: the full ``result_json``
(the agent's own output + the verify-gate output), which the goal layer turns
into the grounded ``deliveries.md`` record the direction evaluator reads.

The :class:`GoalEngine` Protocol is what the goal tick depends on; tests inject a
fake.
"""

from __future__ import annotations

import json
from typing import Optional, Protocol

from .goal_models import Action, Goal, InFlight, PollResult
from .state_store import StateStore, TaskKind
from .task_queue import TaskQueue

_TASK_TERMINAL = {"done", "failed", "cancelled"}
_PROGRAM_TERMINAL = {"done", "failed", "cancelled"}


class GoalEngineError(RuntimeError):
    pass


class GoalEngine(Protocol):
    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight: ...
    async def poll(self, ref: InFlight) -> PollResult: ...


class InProcessEngine:
    """Dispatch goal actions straight into devclaw's own task queue."""

    def __init__(self, queue: TaskQueue, store: StateStore) -> None:
        self._queue = queue
        self._store = store

    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight:
        ws = goal.workspace_dir
        nu = notify_url or None
        if action.tool == "start_program":
            program_id = self._queue.submit_program(
                workspace_dir=ws, goal=action.goal, notify_url=nu
            )
            return InFlight("devclaw", "start_program", program_id, "program", action.goal)
        if action.tool in ("implement_feature", "fix_bug", "review_repository"):
            kind: TaskKind = action.tool  # type: ignore[assignment]
            # review_repository is read-only: no gate, no PR (it writes a report).
            is_review = action.tool == "review_repository"
            task_id = self._queue.submit(
                kind=kind,
                workspace_dir=ws,
                goal=action.goal,
                notify_url=nu,
                verify_cmd=None if is_review else (action.verify_cmd or goal.verify_cmd),
                deliver=False if is_review else action.open_pr,
            )
            return InFlight("devclaw", action.tool, task_id, "task", action.goal)
        raise GoalEngineError(f"unknown engine tool: {action.tool}")

    async def poll(self, ref: InFlight) -> PollResult:
        if ref.ref_kind == "program":
            return self._poll_program(ref.id)
        return self._poll_task(ref.id)

    # ---- internals ---------------------------------------------------------

    def _poll_task(self, task_id: str) -> PollResult:
        t = self._store.get_task(task_id)
        if t is None:
            raise GoalEngineError(f"unknown task_id: {task_id}")
        terminal = t.status in _TASK_TERMINAL
        return PollResult(
            terminal=terminal,
            status=t.status,
            detail=_task_detail(t.kind, t.result_json, t.error, t.pr_url) if terminal else "",
            pr_url=t.pr_url,
            gate_passed=_gate_passed(t.result_json),
        )

    def _poll_program(self, program_id: str) -> PollResult:
        p = self._store.get_program(program_id)
        if p is None:
            raise GoalEngineError(f"unknown program_id: {program_id}")
        terminal = p.status in _PROGRAM_TERMINAL
        tasks = self._store.list_program_tasks(program_id)
        pr_urls = [t.pr_url for t in tasks if t.pr_url]
        detail = ""
        if terminal:
            parts = [f"program {p.status}" + (f" — {p.error}" if p.error else "")]
            for t in tasks:
                parts.append(f"- [{t.status}] {t.goal[:120]}" + (f"  PR {t.pr_url}" if t.pr_url else ""))
            detail = "\n".join(parts)[:4000]
        return PollResult(
            terminal=terminal,
            status=p.status,
            detail=detail,
            pr_url=("; ".join(pr_urls) if pr_urls else None),
            gate_passed=None,  # a program aggregates many gates — no single verdict
        )


def _parse_result(result_json: Optional[str]) -> Optional[dict]:
    if not result_json:
        return None
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _gate_passed(result_json: Optional[str]) -> Optional[bool]:
    """The verify-gate verdict from a finished task's result, if a gate ran."""
    data = _parse_result(result_json)
    verify = data.get("verify") if isinstance(data, dict) else None
    if isinstance(verify, dict) and verify.get("ran") and "passed" in verify:
        return bool(verify["passed"])
    return None


def _task_detail(
    kind: str, result_json: Optional[str], error: Optional[str], pr_url: Optional[str]
) -> str:
    """A grounded, human-readable record of what a finished task actually did —
    the agent's own summary + the gate output + the PR. This is far richer than
    the over-the-wire blob goalclaw used to see; it's what gets written to
    deliveries.md and fed to cognition."""
    data = _parse_result(result_json)
    lines: list[str] = []
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if isinstance(data, dict):
        # Prefer the agent's actual output (the substantive analysis / work
        # summary) over ``message``, which is a generic envelope ("OpenHands
        # completed."). Feeding ``message`` to cognition starved the discovery
        # synthesis, the direction evaluator, and deliveries.md of real signal.
        summary = data.get("agent_output") or data.get("message") or ""
        if isinstance(summary, str) and summary.strip():
            lines.append("Agent summary:\n" + summary.strip()[:6000])
        verify = data.get("verify")
        if isinstance(verify, dict) and verify.get("ran"):
            verdict = "PASSED" if verify.get("passed") else "FAILED"
            out = (verify.get("output") or "").strip()
            lines.append(f"Verify gate `{verify.get('cmd', '')}`: {verdict}")
            if out:
                lines.append("Gate output (tail):\n" + out[-1200:])
    if error:
        lines.append("Error:\n" + error[:1500])
    return "\n\n".join(lines) if lines else f"{kind} finished (no detail captured)"
