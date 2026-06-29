"""The goal layer, wired — the folded-in goalclaw, now a subsystem of devclaw.

Owns the durable goals under ``DEVCLAW_GOALS_DIR``, drives them across a heartbeat
(a resident asyncio loop, woken either by the interval or — in-process — by a task
settling), and exposes the steer/observe surface the MCP tools wrap
(create/get/list/steer/evaluate). Dispatch is in-process via :class:`InProcessEngine`,
so there is no HTTP, no bearer token, and no ``/wake`` endpoint anymore.

Cognition (the planner + the evaluator) is injected; for the live service it
binds devclaw's ``claude --print`` callers at the goal-planner / evaluator tiers.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from . import evaluator as goal_evaluator
from . import merge as goal_merge
from . import planner as goal_planner
from . import research as goal_research
from . import summary as goal_summary
from .engine import InProcessEngine
from .evaluator import ClaudeCaller
from .models import GoalStatus
from .notify import HttpNotifier, Notifier, NullNotifier
from .store import GoalStore
from .tick import EVAL_EVERY, VERIFY_DONE, tick_all, tick_goal
from ..loom import trace as _trace
from ..state_store import StateStore
from ..task_queue import TaskQueue
from ..engine.workspace import prepare_workspace
from .. import trend_detector as _trend_detector_mod


# Telemetry: opt-out via env. Default ON in production so heartbeats leave a
# durable trace in the sqlite traces table; set to "0" for tests / local runs
# where the per-tick PersistentTracer would just be noise. Tests inject their
# own tracer directly when they want to assert on events.
_TRACE_PERSIST_ENABLED = os.environ.get("DEVCLAW_TRACE_PERSIST", "1") != "0"


_BARE_TOOL_RE = re.compile(r"^[^\s/\\]+$")


def _bare_verify_cmd_warning(cmd: str) -> Optional[str]:
    """Return a warning if cmd is a bare tool name (single token, no path separators).
    A name like 'pytest' or 'python' may not be on PATH inside the sandbox."""
    stripped = cmd.strip()
    if stripped and _BARE_TOOL_RE.match(stripped):
        return (
            f"verify_cmd {stripped!r} looks like a bare tool name — it may fail if "
            f"'{stripped}' is not on PATH inside the sandbox. "
            f"Consider 'python -m {stripped}' or a full path instead."
        )
    return None


@dataclass(frozen=True)
class GoalConfig:
    goals_dir: Path
    notify_url: str
    tick_seconds: int
    eval_every: int
    verify_done: bool

    @staticmethod
    def from_env() -> "GoalConfig":
        raw = os.environ.get("DEVCLAW_GOALS_DIR", "~/memory/goals")
        return GoalConfig(
            goals_dir=Path(os.path.expanduser(raw)),
            notify_url=os.environ.get("DEVCLAW_GOAL_NOTIFY_URL", ""),
            tick_seconds=int(os.environ.get("DEVCLAW_GOAL_TICK_SECONDS", "900")),
            eval_every=EVAL_EVERY,
            verify_done=VERIFY_DONE,
        )


class GoalService:
    def __init__(
        self,
        queue: TaskQueue,
        store: StateStore,
        config: Optional[GoalConfig] = None,
        *,
        planner_caller: Optional[ClaudeCaller] = None,
        evaluator_caller: Optional[ClaudeCaller] = None,
        summary_caller: Optional[ClaudeCaller] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self._cfg = config or GoalConfig.from_env()
        self._goal_store = GoalStore(self._cfg.goals_dir)
        self._queue = queue
        self._store = store  # task/event store — read by tail_goal for live events
        self._engine = InProcessEngine(queue, store)
        self._planner_caller = planner_caller  # bound lazily (avoids SDK import in tests)
        self._evaluator_caller = evaluator_caller
        self._summary_caller = summary_caller
        self._notifier: Notifier = notifier or (
            HttpNotifier(self._cfg.notify_url) if self._cfg.notify_url else NullNotifier()
        )
        #: the goal heartbeat task + its in-process wake event
        self._loop_task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        #: trend detector — lazily constructed on first heartbeat that needs it
        #: so tests (which set DEVCLAW_TREND_ENABLED=0 or stub differently) and
        #: cold-starts don't import claude bindings prematurely.
        self._trend_detector_inst: "Optional[_trend_detector_mod.TrendDetector]" = None

    # ---- cognition callers (bound on first real use) -----------------------

    def _planner(self) -> ClaudeCaller:
        if self._planner_caller is None:
            self._planner_caller = goal_planner.default_caller()
        return self._planner_caller

    def _evaluator(self) -> ClaudeCaller:
        if self._evaluator_caller is None:
            self._evaluator_caller = goal_evaluator.default_caller()
        return self._evaluator_caller

    def _summary(self) -> "Optional[ClaudeCaller]":
        """Cheap plain-language summarizer for owner-facing notifications. Off if
        DEVCLAW_GOAL_PLAIN_SUMMARY=0 (then owner messages send raw). Bound lazily."""
        if not goal_summary.PLAIN_SUMMARY_ENABLED:
            return None
        if self._summary_caller is None:
            self._summary_caller = goal_summary.default_caller()
        return self._summary_caller

    def _merger(self) -> "Optional[goal_merge.Merger]":
        """The auto-merger for hands-off delivery (decision 2). None unless
        DEVCLAW_GOAL_AUTOMERGE=1 — merging to the default branch is opt-in."""
        if not goal_merge.AUTOMERGE_ENABLED:
            return None
        return goal_merge.default_merger()

    def _trend_detector(self) -> "Optional[_trend_detector_mod.TrendDetector]":
        """The cross-session trend detector. ``None`` when disabled via
        ``DEVCLAW_TREND_ENABLED=0``. Constructed lazily so tests / cold starts
        don't import the claude bindings until something actually needs them.

        The detector is wired with narrow handles — it can write only to
        ``trends.md``, the sqlite ``meta`` table (cooldown timestamps), the
        ``traces`` table (observability), and the notifier. It has no handle
        to ``GoalStore`` writes, ``TaskQueue.submit``, or any other surface
        that would let it modify goals or AGENTS.md. The boundary is
        structural — see ``devclaw/trend_detector.py`` for the rule."""
        if not _trend_detector_mod.TREND_ENABLED:
            return None
        if self._trend_detector_inst is None:
            from ..planner import claude_with_model

            claude_caller = claude_with_model(
                _trend_detector_mod.TREND_MODEL, role="trend-detector",
            )

            # Fire-and-forget notify shim: TrendDetector calls notifier_send
            # synchronously, but Notifier.send is async. asyncio.create_task
            # detaches the send so the detector doesn't have to await; payload
            # is rendered into a single owner-readable line.
            notifier_inst = self._notifier

            def _notify_send(payload: dict) -> None:
                action = payload.get("proposed_action") or "(none)"
                text = (
                    f"📈 trend: {payload['signal']} ({payload['scope']}) — "
                    f"{payload['observation']}\n"
                    f"proposed action: {action}\n"
                    f"see: {payload['path']}"
                )
                try:
                    asyncio.create_task(notifier_inst.send(text))
                except RuntimeError:
                    # No running loop (e.g. called from sync context in tests).
                    # Drop silently — trends.md still has the entry.
                    pass

            self._trend_detector_inst = _trend_detector_mod.TrendDetector(
                state_store=self._store,
                goals_dir=self._cfg.goals_dir,
                claude_caller=claude_caller,
                notifier_send=_notify_send,
            )
        return self._trend_detector_inst

    def read_trends(self, scope: str = "harness_self", limit_chars: int = 5000) -> dict:
        """Read recent trend observations from ``trends.md`` for a given scope.

        ``scope='harness_self'`` → the global harness-self file (defaults into
        Denys's vault per ``DEVCLAW_TREND_HARNESS_SELF_FILE``).

        Anything else is treated as a workspace path → reads
        ``<scope>/.devclaw/trends.md``."""
        from ..trend_detector import HARNESS_SELF_TRENDS_PATH

        if scope == "harness_self":
            path = HARNESS_SELF_TRENDS_PATH
        else:
            path = Path(scope) / ".devclaw" / "trends.md"
        text: str
        if not path.exists():
            text = "(no trends recorded for this scope yet)"
        else:
            try:
                raw = path.read_text()
            except OSError as exc:
                text = f"(could not read {path}: {exc})"
            else:
                text = raw[-limit_chars:] if len(raw) > limit_chars else raw
        return {"scope": scope, "path": str(path), "trends": text}

    # ---- the heartbeat -----------------------------------------------------

    def start(self) -> None:
        """Start the resident goal heartbeat. Idempotent. Called by the server
        after the task queue starts ticking."""
        if self._loop_task is None or self._loop_task.done():
            self._wake = asyncio.Event()
            self._loop_task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def poke(self) -> None:
        """Wake the heartbeat NOW — wired to the task queue's on-settle hook so a
        finished engine task triggers an immediate goal tick (the in-process
        replacement for the old HTTP /wake). Safe to call from the event loop."""
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        n = len(self._goal_store.list_goal_ids())
        sys.stderr.write(
            f"goal-layer: heartbeat {self._cfg.tick_seconds}s over {self._cfg.goals_dir} "
            f"({n} goal(s))\n"
        )
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._cfg.tick_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                outcomes = await self.tick_all()
                # A lifecycle transition (discovery→executing, plan_review→executing)
                # returns ADVANCED to signal "something changed but no task was
                # dispatched yet." Re-poke immediately so the next planning tick
                # starts without waiting the full 900s heartbeat interval.
                if any(v == "advanced" for v in outcomes.values()):
                    self.poke()
            except Exception as exc:  # noqa: BLE001 — a tick crash must not kill the loop
                sys.stderr.write(f"goal-layer: tick crashed: {exc}\n")

    def _make_tracer(self, goal_id: str) -> "Optional[_trace.PersistentTracer]":
        """Per-goal-tick PersistentTracer that writes into the sqlite traces
        table. Disabled when DEVCLAW_TRACE_PERSIST=0 (test/local convenience).
        Each tick gets a fresh ``trace_id`` so the full causal chain of one
        wakeup can be replayed via ``get_trace(goal_id)``.
        """
        if not _TRACE_PERSIST_ENABLED:
            return None
        return _trace.PersistentTracer(
            store=self._store,
            trace_id=str(uuid.uuid4()),
            goal_id=goal_id,
            label=f"tick-{goal_id}",
        )

    async def tick_all(self) -> dict:
        outcomes = await tick_all(
            store=self._goal_store, engine=self._engine,
            planner_caller=self._planner(), evaluator_caller=self._evaluator(),
            notifier=self._notifier, notify_url="",
            eval_every=self._cfg.eval_every, verify_done=self._cfg.verify_done,
            summary_caller=self._summary(), merger=self._merger(),
            tracer_factory=self._make_tracer,
            trend_detector=self._trend_detector(),
        )
        return {gid: o.value for gid, o in outcomes.items()}

    async def tick_one(self, goal_id: str) -> str:
        with _trace.tracer_scope(self._make_tracer(goal_id)):
            outcome = await tick_goal(
                goal_id, store=self._goal_store, engine=self._engine,
                planner_caller=self._planner(), evaluator_caller=self._evaluator(),
                notifier=self._notifier, notify_url="",
                eval_every=self._cfg.eval_every, verify_done=self._cfg.verify_done,
                summary_caller=self._summary(), merger=self._merger(),
                trend_detector=self._trend_detector(),
            )
        return outcome.value

    # ---- steer / observe surface (wrapped by MCP tools) --------------------

    def create_goal(
        self, goal_id: str, *, objective: str, workspace_dir: str,
        cadence: str = "1d", repo_url: Optional[str] = None,
        verify_cmd: Optional[str] = None, open_pr: bool = True,
        done_when: str = "", backlog: Optional[list[str]] = None,
        spec: str = "",
    ) -> dict:
        goal = self._goal_store.create_goal(
            goal_id, objective=objective, workspace_dir=workspace_dir, cadence=cadence,
            repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
            done_when=done_when, backlog=backlog,
        )
        # The waiter may have grilled scope before filing the order — persist the
        # spec it landed on so the evaluator judges done against the shared contract.
        if spec and spec.strip():
            self._goal_store.write_spec(goal_id, spec)
        # Outcome goals investigate (research → discovery brief) before executing;
        # stamp the starting lifecycle so the first tick opens that front-end.
        if goal_research.INVESTIGATE_ENABLED:
            self._goal_store.save_status(goal_id, GoalStatus(lifecycle="investigating"))
        self._goal_store.append_log(goal_id, "goal created")
        self.poke()  # advance it on the next loop turn without waiting a full interval
        result = self.get_goal(goal_id)
        if verify_cmd:
            warning = _bare_verify_cmd_warning(verify_cmd)
            if warning:
                result["warnings"] = [warning]
        return result

    def get_goal(self, goal_id: str) -> dict:
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        # Effective goal so the MCP-exposed view shows what cognition actually
        # uses (firmed-derived done_when, firmed stub_acceptable) — not the
        # original owner statement that's already been firmed past.
        g = self._goal_store.load_effective_goal(goal_id)
        s = self._goal_store.load_status(goal_id)
        firmed_draft = self._firmed_draft_payload(goal_id)
        return {
            "id": g.id,
            "objective": g.objective,
            "done_when": g.done_when,
            "cadence": g.cadence,
            "workspace_dir": g.workspace_dir,
            "backlog": g.backlog,
            "phase": s.phase,
            "lifecycle": s.lifecycle or "executing",
            "next": s.next,
            "blocked_on": s.blocked_on,
            "in_flight": (
                {"tool": s.in_flight.tool, "id": s.in_flight.id,
                 "is_done_check": s.in_flight.is_done_check}
                if s.in_flight else None
            ),
            "actions_dispatched": s.actions_dispatched,
            "progress": {"last_at": s.last_progress_at, "stalled": s.no_progress_notified},
            "direction": (
                {"verdict": s.last_eval_verdict, "at": s.last_eval_at, "note": s.last_eval_note}
                if s.last_eval_verdict else None
            ),
            "recent_log": self._goal_store.recent_log(goal_id, n=15),
            "firmed_draft": firmed_draft,
        }

    def _firmed_draft_payload(self, goal_id: str) -> Optional[dict]:
        """Serialize the goal's current firmed-draft for the waiter — only the
        fields the waiter renders (status, round, unknowns + their options/why,
        success_criteria). Returns None when firming hasn't run yet."""
        draft = self._goal_store.read_firmed_draft(goal_id)
        if draft is None:
            return None
        return {
            "status": draft.status,
            "round": draft.round,
            "intent": draft.intent,
            "success_criteria": [
                {"id": c.id, "text": c.text, "verifiable_by": c.verifiable_by}
                for c in draft.success_criteria
            ],
            "conventions_to_follow": list(draft.conventions_to_follow),
            "unknowns": [
                {
                    "id": u.id, "question": u.question, "why": u.why,
                    "options": list(u.options),
                    "default_if_no_answer": u.default_if_no_answer,
                }
                for u in draft.unknowns
            ],
            "blockers": list(draft.blockers),
            "stub_acceptable": list(draft.stub_acceptable),
            "descoped": list(draft.descoped),
        }

    def tail_goal(
        self,
        goal_id: str,
        *,
        log_lines: int = 40,
        deliveries_chars: int = 6000,
        event_limit: int = 30,
    ) -> dict:
        """The 'watch it run' surface — richer than get_goal, no SSH needed. On top
        of get_goal's phase/direction/log it returns the grounded deliveries tail
        (what each action actually shipped), the discovery brief + any waiter-
        provided spec, and the tail of the LIVE event stream from whatever
        task/program is in flight (so you can see the agent acting in near real
        time). Everything is bounded — read-only, never mutates the goal."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        g = self._goal_store.load_effective_goal(goal_id)
        s = self._goal_store.load_status(goal_id)

        live_events: list[dict] = []
        if s.in_flight is not None:
            ref = s.in_flight
            kwargs = (
                {"task_id": ref.id} if ref.ref_kind == "task" else {"program_id": ref.id}
            )
            # list_events is ASC + LIMIT (first N); pull a wide window and tail it
            # in Python so we get the MOST RECENT events of a long-running task.
            evs = self._store.list_events(limit=10000, **kwargs)
            for e in evs[-event_limit:]:
                preview = (e.payload_json or "")[:200]
                live_events.append(
                    {"type": e.type, "source": e.source, "ts": e.ts, "preview": preview}
                )

        return {
            "id": g.id,
            "objective": g.objective,
            "done_when": g.done_when,
            "phase": s.phase,
            "lifecycle": s.lifecycle or "executing",
            "next": s.next,
            "blocked_on": s.blocked_on,
            "actions_dispatched": s.actions_dispatched,
            "in_flight": (
                {"tool": s.in_flight.tool, "id": s.in_flight.id,
                 "ref_kind": s.in_flight.ref_kind,
                 "is_done_check": s.in_flight.is_done_check}
                if s.in_flight else None
            ),
            "progress": {"last_at": s.last_progress_at, "stalled": s.no_progress_notified},
            "direction": (
                {"verdict": s.last_eval_verdict, "at": s.last_eval_at,
                 "note": s.last_eval_note}
                if s.last_eval_verdict else None
            ),
            "recent_log": self._goal_store.recent_log(goal_id, n=log_lines),
            "deliveries": self._goal_store.recent_deliveries(goal_id, chars=deliveries_chars),
            "discovery": self._goal_store.read_discovery(goal_id),
            "spec": self._goal_store.read_spec(goal_id),
            "live_events": live_events,
        }

    def list_goals(self) -> list[dict]:
        out = []
        for gid in self._goal_store.list_goal_ids():
            g = self._goal_store.load_goal(gid)
            s = self._goal_store.load_status(gid)
            out.append({
                "id": gid,
                "objective": g.objective[:140],
                "phase": s.phase,
                "lifecycle": s.lifecycle or "executing",
                "direction": s.last_eval_verdict,
                "actions_dispatched": s.actions_dispatched,
            })
        return out

    def steer_goal(self, goal_id: str, message: str) -> dict:
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        self._goal_store.append_steering(goal_id, [message], source="denys")
        self._goal_store.append_log(goal_id, f"steered: {message[:160]}")
        # Steering unblocks a blocked goal — flip it to idle and clear the
        # dispatch counter so the cap doesn't re-trigger on the very next tick.
        s = self._goal_store.load_status(goal_id)
        if s.phase == "blocked":
            self._goal_store.save_status(goal_id, replace(s, phase="idle", actions_dispatched=0))
        self.poke()
        return {"goal_id": goal_id, "steered": True, "message": message}

    async def evaluate_goal(self, goal_id: str) -> dict:
        """Force a direction evaluation NOW (artifact-grounded) and return the
        verdict. Reports + steers (corrections → inbox); does not block on demand."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        # Effective goal so the evaluator's stub-policy check honors any
        # stub_acceptable the owner added during firming.
        g = self._goal_store.load_effective_goal(goal_id)
        s = self._goal_store.load_status(goal_id)
        ev = await goal_evaluator.evaluate(
            g, s, self._goal_store.recent_log(goal_id),
            self._goal_store.recent_deliveries(goal_id),
            claude_caller=self._evaluator(),
        )
        now = self._goal_store.now_iso()
        self._goal_store.save_status(goal_id, replace(
            s, last_eval_verdict=ev.verdict, last_eval_at=now, last_eval_note=ev.rationale[:300],
        ))
        self._goal_store.append_log(goal_id, f"on-demand direction: {ev.verdict} — {ev.rationale[:200]}")
        if ev.corrections:
            self._goal_store.append_steering(goal_id, ev.corrections, source="auto-eval")
            self.poke()
        return {
            "goal_id": goal_id, "verdict": ev.verdict,
            "rationale": ev.rationale, "corrections": ev.corrections,
            "question": ev.question,
        }

    async def answer_unknowns(self, goal_id: str, answers: dict[str, str]) -> dict:
        """Owner-side input for the firming phase. The MCP tool wraps this.
        Validates that ``answers`` covers every current unknown id (no partials,
        no extras), then fires firming round N+1 via the FirmingHandler. Returns
        the structured next state (``firmed`` or ``needs_more_answers``)."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        draft = self._goal_store.read_firmed_draft(goal_id)
        if draft is None:
            raise ValueError(
                f"goal {goal_id!r} has no firmed-draft.yaml yet — firming round 1 "
                "must run first (created via the heartbeat after discovery completes)"
            )
        expected = {u.id for u in draft.unknowns}
        provided = {k for k, v in (answers or {}).items() if str(v).strip()}
        missing = expected - provided
        extra = provided - expected
        if missing or extra:
            raise ValueError(
                f"answers must cover EVERY current unknown exactly once — "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

        from .phases.firming import FirmingHandler
        from .phases.registry import handler_for
        from .tick import TickContext

        handler = handler_for("firming")
        if not isinstance(handler, FirmingHandler):
            raise RuntimeError("firming handler is not registered")
        ctx = TickContext(
            store=self._goal_store, engine=self._engine,
            planner_caller=self._planner(), evaluator_caller=self._evaluator(),
            notifier=self._notifier, notify_url=self._cfg.notify_url,
            eval_every=self._cfg.eval_every, verify_done=self._cfg.verify_done,
            summary_caller=self._summary(), merger=self._merger(),
        )
        result = await handler.handle_answer(goal_id, answers, ctx=ctx)
        # Decomposer + first executor tick fire on the next heartbeat; poke it now
        # so a firmed goal starts working immediately rather than waiting 900s.
        self.poke()
        return result

    def cancel_goal(self, goal_id: str) -> dict:
        """Abort a durable goal. Sets phase to 'cancelled' (terminal — skipped on
        every future tick) and tears down any in-flight task or program. Returns
        a graceful no-op response if the goal is already in a terminal phase."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        s = self._goal_store.load_status(goal_id)
        if s.phase in ("cancelled", "done"):
            return {
                "goal_id": goal_id,
                "cancelled": False,
                "phase": s.phase,
                "reason": f"goal is already in terminal phase '{s.phase}'",
            }
        if s.in_flight is not None:
            ref = s.in_flight
            if ref.ref_kind == "task":
                self._queue.cancel_task(ref.id)
            else:
                self._queue.cancel_program(ref.id)
        self._goal_store.save_status(goal_id, replace(s, phase="cancelled", in_flight=None))
        self._goal_store.append_log(goal_id, "goal cancelled")
        return {"goal_id": goal_id, "cancelled": True, "phase": "cancelled"}
