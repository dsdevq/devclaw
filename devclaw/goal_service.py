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
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from . import goal_evaluator, goal_grill, goal_merge, goal_planner, goal_research, goal_summary
from .goal_engine import InProcessEngine
from .goal_evaluator import ClaudeCaller
from .goal_models import GoalStatus
from .goal_notify import HttpNotifier, Notifier, NullNotifier
from .goal_store import GoalStore
from .goal_tick import EVAL_EVERY, VERIFY_DONE, tick_all, tick_goal
from .state_store import StateStore
from .task_queue import TaskQueue
from .workspace import prepare_workspace


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
        self._engine = InProcessEngine(queue, store)
        self._planner_caller = planner_caller  # bound lazily (avoids SDK import in tests)
        self._evaluator_caller = evaluator_caller
        self._summary_caller = summary_caller
        self._grill_caller: Optional[ClaudeCaller] = None
        self._notifier: Notifier = notifier or (
            HttpNotifier(self._cfg.notify_url) if self._cfg.notify_url else NullNotifier()
        )
        #: the goal heartbeat task + its in-process wake event
        self._loop_task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None

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

    def _grill(self) -> "Optional[ClaudeCaller]":
        """The grill cognition for the grilling phase. None unless
        DEVCLAW_GOAL_GRILL=1 (the Telegram answer channel must be wired first)."""
        if not goal_grill.GRILL_ENABLED:
            return None
        if self._grill_caller is None:
            self._grill_caller = goal_grill.default_caller()
        return self._grill_caller

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
                await self.tick_all()
            except Exception as exc:  # noqa: BLE001 — a tick crash must not kill the loop
                sys.stderr.write(f"goal-layer: tick crashed: {exc}\n")

    async def tick_all(self) -> dict:
        outcomes = await tick_all(
            store=self._goal_store, engine=self._engine,
            planner_caller=self._planner(), evaluator_caller=self._evaluator(),
            notifier=self._notifier, notify_url="",
            eval_every=self._cfg.eval_every, verify_done=self._cfg.verify_done,
            summary_caller=self._summary(), merger=self._merger(), grill_caller=self._grill(),
        )
        return {gid: o.value for gid, o in outcomes.items()}

    async def tick_one(self, goal_id: str) -> str:
        outcome = await tick_goal(
            goal_id, store=self._goal_store, engine=self._engine,
            planner_caller=self._planner(), evaluator_caller=self._evaluator(),
            notifier=self._notifier, notify_url="",
            eval_every=self._cfg.eval_every, verify_done=self._cfg.verify_done,
            summary_caller=self._summary(), merger=self._merger(), grill_caller=self._grill(),
        )
        return outcome.value

    # ---- steer / observe surface (wrapped by MCP tools) --------------------

    def create_goal(
        self, goal_id: str, *, objective: str, workspace_dir: str,
        cadence: str = "1d", repo_url: Optional[str] = None,
        verify_cmd: Optional[str] = None, open_pr: bool = True,
        done_when: str = "", backlog: Optional[list[str]] = None,
    ) -> dict:
        goal = self._goal_store.create_goal(
            goal_id, objective=objective, workspace_dir=workspace_dir, cadence=cadence,
            repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
            done_when=done_when, backlog=backlog,
        )
        # Outcome goals investigate (research → discovery brief) before executing;
        # stamp the starting lifecycle so the first tick opens that front-end.
        if goal_research.INVESTIGATE_ENABLED:
            self._goal_store.save_status(goal_id, GoalStatus(lifecycle="new"))
        self._goal_store.append_log(goal_id, "goal created")
        self.poke()  # advance it on the next loop turn without waiting a full interval
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> dict:
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        g = self._goal_store.load_goal(goal_id)
        s = self._goal_store.load_status(goal_id)
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
            "direction": (
                {"verdict": s.last_eval_verdict, "at": s.last_eval_at, "note": s.last_eval_note}
                if s.last_eval_verdict else None
            ),
            "recent_log": self._goal_store.recent_log(goal_id, n=15),
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
        # Steering unblocks a blocked goal — flip it so the next tick re-plans.
        s = self._goal_store.load_status(goal_id)
        if s.phase == "blocked":
            self._goal_store.save_status(goal_id, replace(s, phase="idle"))
        self.poke()
        return {"goal_id": goal_id, "steered": True, "message": message}

    def answer_goal(self, goal_id: str, answer: str) -> dict:
        """Route an owner's reply (from Telegram) back to a goal awaiting input.
        In ``grilling`` it answers the open grill question; in ``plan_review`` it
        approves the plan. The next tick (woken via poke) advances the goal."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        s = self._goal_store.load_status(goal_id)
        lifecycle = s.lifecycle or "executing"
        if lifecycle == "grilling":
            recorded = self._goal_store.answer_pending(goal_id, answer)
            self._goal_store.append_log(goal_id, f"grill answer: {answer[:160]}")
            self.poke()
            return {"goal_id": goal_id, "routed_to": "grill", "recorded": recorded}
        if lifecycle == "plan_review":
            self._goal_store.mark_plan_approved(goal_id)
            self._goal_store.append_log(goal_id, f"plan approved: {answer[:160]}")
            self.poke()
            return {"goal_id": goal_id, "routed_to": "plan_approval", "approved": True}
        return {"goal_id": goal_id, "routed_to": None,
                "error": f"goal is not awaiting input (lifecycle={lifecycle})"}

    async def evaluate_goal(self, goal_id: str) -> dict:
        """Force a direction evaluation NOW (artifact-grounded) and return the
        verdict. Reports + steers (corrections → inbox); does not block on demand."""
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        g = self._goal_store.load_goal(goal_id)
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
