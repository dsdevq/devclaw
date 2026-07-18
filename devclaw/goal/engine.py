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

from .models import Action, Goal, InFlight, PollResult
from ..state_store import StateStore, TaskKind
from ..task_queue import TaskQueue

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

    @property
    def kind(self) -> str:
        """Pass-through to the task queue's engine label ("stub" / "sandcastle"
        / "host" / "claude_sdk"). Used by trace recorders so the timeline
        shows which engine actually ran each dispatch."""
        return getattr(self._queue, "engine_kind", "unknown")

    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight:
        """Dispatch ONE action. ``pump=False`` on both submit calls (PR7 —
        the dispatch/pump split): this method runs INSIDE the goal tick's
        atomic dispatch transaction (see ``tick._run_atomic``), which holds
        the shared StateStore's lock for its whole extent. The row-creation
        write joins that transaction (and rolls back with it on a CAS
        conflict or a later raise); it must NOT also claim + launch — that
        synchronously mutates OTHER, unrelated pending rows as part of the
        same atomic unit and spawns real background execution for them that
        a rollback cannot undo (a phantom container running against a row
        that no longer exists). The caller (tick.py) explicitly kicks the
        queue via ``kick()`` AFTER its transaction commits."""
        ws = goal.workspace_dir
        nu = notify_url or None
        if action.tool == "start_program":
            # Program-child tasks inherit ``open_pr`` and ``verify_cmd`` — the
            # standing-goal / reviewable-slice contract. Under a mission goal
            # (``open_pr: true``), the decomposer's tasks each deliver as a
            # PR instead of committing straight to the workspace branch.
            # Closes the 2026-07-03 closeloop-mission-v2 defect where the
            # activity-timeline program pushed direct-to-main because the
            # flags stopped at ``submit_program``.
            program_id = self._queue.submit_program(
                workspace_dir=ws, goal=action.goal, notify_url=nu,
                open_pr=action.open_pr,
                verify_cmd=action.verify_cmd or goal.verify_cmd,
                parent_goal_id=goal.id,
                pump=False,
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
                title=None if is_review else action.title,
                parent_goal_id=goal.id,
                # L3 (#222): a pure-scaffolding item skips the adversarial review
                # gate (verified structurally by the build gate instead). Never
                # for a read-only review_repository — it has no diff to review.
                scaffold=False if is_review else action.scaffold,
                pump=False,
            )
            return InFlight("devclaw", action.tool, task_id, "task", action.goal)
        raise GoalEngineError(f"unknown engine tool: {action.tool}")

    def kick(self) -> None:
        """Nudge the task queue to claim + launch any pending row NOW, rather
        than wait for its periodic pump (``TaskQueue.start_ticking``'s
        ``TICK_SECONDS`` loop). Called by the goal tick AFTER its dispatch
        transaction commits (see ``tick._engine_kick``) — a crash between
        commit and kick is self-healing: the row is durably 'pending' either
        way, and the queue's own heartbeat pumps it within one tick."""
        self._queue.pump()

    async def poll(self, ref: InFlight) -> PollResult:
        if ref.ref_kind == "program":
            return self._poll_program(ref.id)
        return self._poll_task(ref.id)

    def latest_program_for_goal(self, goal_id: str) -> Optional[tuple[str, str]]:
        """(program_id, program_goal) of the goal's most recent program, or
        None. The orphan sweep (``tick.sweep_orphaned_refs``) uses this to
        rediscover a program whose in-flight ref was lost from STATUS.md —
        without it a crash mid-status-write silently divorces a goal from
        its own running (or already-failed) program and the goal waits
        forever."""
        p = self._store.latest_program_for_goal(goal_id)
        return (p.id, p.goal) if p is not None else None

    def latest_task_for_goal(self, goal_id: str) -> Optional[tuple[str, str, str]]:
        """(task_id, task_goal, task_kind) of the goal's most recent task, or
        None. Mirrors :meth:`latest_program_for_goal` — the orphan sweep's
        finder for the TASK half of a lost in-flight ref (PR7 extends
        re-adoption from programs-only to both). ``task_kind`` (e.g.
        ``implement_feature``) is what the sweep uses to rebuild the
        InFlight's ``tool`` field."""
        t = self._store.latest_task_for_goal(goal_id)
        return (t.id, t.goal, t.kind) if t is not None else None

    # ---- shared quota pause (same flag the task queue honours) --------------
    # The OAuth quota is account-wide, so the goal heartbeat and the task queue
    # pause as one. These delegate to the single StateStore flag.

    def global_pause(self) -> tuple[int, str]:
        return self._store.global_pause()

    def set_global_pause(self, until_ms: int, reason: str) -> None:
        self._store.set_global_pause(until_ms, reason)

    def clear_global_pause(self) -> None:
        self._store.clear_global_pause()

    def pause_notified(self) -> bool:
        """Whether the owner was already pinged about the current quota pause
        (the goal tick pings once per pause + once on resume, not every tick)."""
        return self._store.pause_notified()

    def set_pause_notified(self, on: bool) -> None:
        self._store.set_pause_notified(on)

    def operator_block(self, now_ms: int) -> tuple[bool, str]:
        """The manual-hold + daily run-window gate (``dispatch_gate.operator_block``),
        read by the goal heartbeat beside the quota pause. Delegates to the same
        StateStore the task queue reads, so both loops gate identically."""
        from ..dispatch_gate import operator_block
        return operator_block(
            self._store.operator_hold(), self._store.get_run_schedule(), now_ms
        )

    def prune_traces(self) -> int:
        """Daily trace-retention prune (volume hygiene, 2026-07-15). Delegates
        to :meth:`StateStore.maybe_prune_traces` — the store owns all trace
        writes, so the engine is only the seam the goal heartbeat reaches it
        through (same getattr pattern as the quota-pause accessors above;
        test doubles without this method mean no prune, harmlessly)."""
        return self._store.maybe_prune_traces()

    def prune_events(self) -> int:
        """Daily events-retention prune (volume hygiene, 2026-07-18). Delegates
        to :meth:`StateStore.maybe_prune_events` — same seam as
        :meth:`prune_traces`, bounding the highest-volume append-only log after
        traces (raw runner SDK events)."""
        return self._store.maybe_prune_events()

    def vacuum(self) -> bool:
        """Weekly VACUUM that reclaims the disk the retention prunes free
        (volume hygiene, 2026-07-18). Delegates to :meth:`StateStore.maybe_vacuum`
        — same getattr seam as :meth:`prune_traces`."""
        return self._store.maybe_vacuum()

    def check_db_size_alert(self) -> "str | None":
        """One-shot DB-size alarm (loud-not-silent, 2026-07-18). Delegates to
        :meth:`StateStore.check_db_size_alert` — same getattr seam as
        :meth:`vacuum`. Returns an owner-facing message the tick the .db crosses
        the threshold, else None. The goal layer owns the Notifier, so the
        actual ping happens at the tick call site, not here."""
        return self._store.check_db_size_alert()

    def goal_operator_block(self, goal_id: str, now_ms: int) -> tuple[bool, str]:
        """A single goal's OWN run-window gate — applied on top of the engine-wide
        :meth:`operator_block` so a goal dispatches only if the global controls AND
        its own window both allow it. Schedule-only (a person pausing everything
        uses the global hold); an unset per-goal window never blocks (fail-open)."""
        from ..dispatch_gate import schedule_blocks
        return schedule_blocks(self._store.get_run_schedule(goal_id), now_ms)

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


#: How much of ``agent_output`` to keep when building task detail. For most
#: kinds this is a work summary written to deliveries.md (which the planner
#: reads next tick) — 6 KB is plenty. But ``review_repository`` agent_output
#: IS the report the done-gate evaluator judges against; truncating it at 6 KB
#: kept only the SDK's user-message panel echoing the brief (which contains
#: literal ``<clause 1 text>`` placeholders in its format spec) plus a few
#: early `status=pending` tool calls. The evaluator then judged the empty
#: template, the per-clause extractor in ``evaluator._extract_review_report``
#: never saw the filled section because it didn't exist in the truncated
#: input. Keep the full transcript for reviews so the extractor can find the
#: actual filled report at its end (typically 60–160 KB total).
_TASK_DETAIL_SUMMARY_KEEP = {"review_repository": 200_000}


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
            keep = _TASK_DETAIL_SUMMARY_KEEP.get(kind, 6000)
            lines.append("Agent summary:\n" + summary.strip()[:keep])
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
