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
from typing import TYPE_CHECKING, Callable, Optional

from . import evaluator as goal_evaluator
from . import merge as goal_merge
from . import planner as goal_planner
from . import remote_checks as goal_remote_checks
from . import research as goal_research
from . import summary as goal_summary
from .engine import InProcessEngine
from .evaluator import ClaudeCaller
from .models import Goal, GoalStatus
from .notify import HttpNotifier, Notifier, NullNotifier
from .store import GoalStore
from .tick import AUTODEPLOY_ENABLED, EVAL_EVERY, VERIFY_DONE, tick_all, tick_goal
from ..loom import trace as _trace
from ..state_store import StateStore
from ..task_queue import TaskQueue
from ..engine.workspace import prepare_workspace
from .. import trend_detector as _trend_detector_mod

if TYPE_CHECKING:
    from ..project_registry import ProjectRegistry


# Telemetry: opt-out via env. Default ON in production so heartbeats leave a
# durable trace in the sqlite traces table; set to "0" for tests / local runs
# where the per-tick PersistentTracer would just be noise. Tests inject their
# own tracer directly when they want to assert on events.
_TRACE_PERSIST_ENABLED = os.environ.get("DEVCLAW_TRACE_PERSIST", "1") != "0"


_BARE_TOOL_RE = re.compile(r"^[^\s/\\]+$")


def _bare_verify_cmd_warning(cmd: str) -> Optional[str]:
    """DEPRECATED — kept temporarily for external callers / back-compat.
    The check moved into :mod:`devclaw.goal.admission` as one of several
    structured conditions ``verify_goal`` returns. New code should call
    ``verify_goal`` directly and route on the ``bare_verify_cmd`` code."""
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
    autodeploy: bool = AUTODEPLOY_ENABLED

    @staticmethod
    def from_env() -> "GoalConfig":
        raw = os.environ.get("DEVCLAW_GOALS_DIR", "~/memory/goals")
        return GoalConfig(
            goals_dir=Path(os.path.expanduser(raw)),
            notify_url=os.environ.get("DEVCLAW_GOAL_NOTIFY_URL", ""),
            tick_seconds=int(os.environ.get("DEVCLAW_GOAL_TICK_SECONDS", "900")),
            eval_every=EVAL_EVERY,
            verify_done=VERIFY_DONE,
            autodeploy=AUTODEPLOY_ENABLED,
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
        project_registry: "Optional[ProjectRegistry]" = None,
    ) -> None:
        self._cfg = config or GoalConfig.from_env()
        self._goal_store = GoalStore(self._cfg.goals_dir)
        self._queue = queue
        self._store = store  # task/event store — read by tail_goal for live events
        self._engine = InProcessEngine(queue, store)
        self._planner_caller = planner_caller  # bound lazily (avoids SDK import in tests)
        self._evaluator_caller = evaluator_caller
        self._summary_caller = summary_caller
        #: used only to resolve per-project automerge overrides (see _merger).
        #: None is fine — automerge just falls back to the global default.
        self._project_registry = project_registry
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

    def _merger(self, goal: "Optional[Goal]" = None) -> "Optional[goal_merge.Merger]":
        """The auto-merger for hands-off delivery (decision 2) — resolved for
        THIS goal's repo: its owning project's ``automerge`` override if one is
        set, else the devclaw-wide ``DEVCLAW_GOAL_AUTOMERGE`` default. Merging
        to the default branch is opt-in either way. ``goal=None`` (e.g. the
        firming phase, which never merges) just falls back to the global
        default since there's no workspace to look up a project by."""
        workspace_dir = goal.workspace_dir if goal is not None else None
        if not goal_merge.resolve_automerge(self._project_registry, workspace_dir):
            return None
        strategy = goal_merge.resolve_merge_strategy(self._project_registry, workspace_dir)
        return goal_merge.default_merger(strategy)

    def _merger_resolver(self) -> "Callable[[Goal], Optional[goal_merge.Merger]]":
        """Bound for tick_all, which ticks every goal in one sweep and needs a
        fresh per-goal automerge decision rather than one value for the whole
        fleet (a project override for goal A must not leak onto goal B)."""
        return self._merger

    def _remote_checker(self) -> "Optional[goal_remote_checks.RemoteChecker]":
        """Grounded remote-checks verification at the done-gate (the 2026-07-06
        benchmark fix). On by default; DEVCLAW_GOAL_REMOTE_CHECKS=0 disables —
        the checker itself fails open on infra errors, so opting out is only
        for environments with no gh at all."""
        if not goal_remote_checks.REMOTE_CHECKS_ENABLED:
            return None
        return goal_remote_checks.default_checker()

    def _verify_done(self, goal: "Optional[Goal]" = None) -> bool:
        """The done-gate re-check policy for THIS goal's repo: its owning
        project's ``verify_done`` override if set, else the devclaw-wide
        ``DEVCLAW_GOAL_VERIFY_DONE`` default (carried on the config). ``goal=None``
        or no registry → the global default."""
        default = self._cfg.verify_done
        if self._project_registry is None or goal is None:
            return default
        return self._project_registry.resolve_override(
            goal.workspace_dir, "verify_done", default
        )

    def _verify_done_resolver(self) -> "Callable[[Goal], bool]":
        """Per-goal ``verify_done`` for tick_all's sweep — a project override
        for one goal must not leak onto another (same reason as
        :meth:`_merger_resolver`)."""
        return self._verify_done

    def _autodeploy(self, goal: "Optional[Goal]" = None) -> bool:
        """The on-complete auto-deploy policy for THIS goal's repo: its owning
        project's ``autodeploy`` override if set, else the devclaw-wide
        ``DEVCLAW_GOAL_AUTODEPLOY`` default (carried on the config)."""
        default = self._cfg.autodeploy
        if self._project_registry is None or goal is None:
            return default
        return self._project_registry.resolve_override(
            goal.workspace_dir, "autodeploy", default
        )

    def _autodeploy_resolver(self) -> "Callable[[Goal], bool]":
        """Per-goal ``autodeploy`` for tick_all's sweep (same reason as
        :meth:`_merger_resolver`)."""
        return self._autodeploy

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
        ``<scope>/.devclaw/trends.md``.

        The actual read is delegated to ``trend_detector.read_trends_text`` so
        the same primitive feeds both this MCP wrapper and the per-tick prompt
        injection in ``goal/tick.py``."""
        from ..trend_detector import HARNESS_SELF_TRENDS_PATH, read_trends_text

        if scope == "harness_self":
            path = HARNESS_SELF_TRENDS_PATH
        else:
            path = Path(scope) / ".devclaw" / "trends.md"
        text = read_trends_text(scope, limit_chars)
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
            verify_done_resolver=self._verify_done_resolver(),
            autodeploy=self._cfg.autodeploy, autodeploy_resolver=self._autodeploy_resolver(),
            summary_caller=self._summary(), merger_resolver=self._merger_resolver(),
            tracer_factory=self._make_tracer,
            trend_detector=self._trend_detector(),
            remote_checker=self._remote_checker(),
        )
        return {gid: o.value for gid, o in outcomes.items()}

    async def tick_one(self, goal_id: str) -> str:
        goal = self._goal_store.load_goal(goal_id)
        with _trace.tracer_scope(self._make_tracer(goal_id)):
            outcome = await tick_goal(
                goal_id, store=self._goal_store, engine=self._engine,
                planner_caller=self._planner(), evaluator_caller=self._evaluator(),
                notifier=self._notifier, notify_url="",
                eval_every=self._cfg.eval_every, verify_done=self._verify_done(goal),
                autodeploy=self._autodeploy(goal),
                summary_caller=self._summary(), merger=self._merger(goal),
                trend_detector=self._trend_detector(),
                remote_checker=self._remote_checker(),
            )
        return outcome.value

    # ---- steer / observe surface (wrapped by MCP tools) --------------------

    def create_goal(
        self, goal_id: str, *, objective: str, workspace_dir: str,
        cadence: str = "1d", repo_url: Optional[str] = None,
        verify_cmd: Optional[str] = None, open_pr: bool = True,
        done_when: str = "", backlog: Optional[list[str]] = None,
        spec: str = "", skills_required: Optional[list[str]] = None,
    ) -> dict:
        # Chef admission ("verified on all sides"). Goals that fail structural
        # checks are REJECTED with a structured condition list — the caller
        # (waiter or upstream chain) must fix and re-file. Warnings still flow
        # through to the result dict as before. See devclaw/goal/admission.py.
        from .admission import GoalAdmissionRejected, verify_goal as _verify

        admission = _verify(
            objective=objective, workspace_dir=workspace_dir, done_when=done_when,
            backlog=backlog, repo_url=repo_url, verify_cmd=verify_cmd, spec=spec,
            skills_required=skills_required,
        )
        if not admission.admitted:
            raise GoalAdmissionRejected(admission)

        goal = self._goal_store.create_goal(
            goal_id, objective=objective, workspace_dir=workspace_dir, cadence=cadence,
            repo_url=repo_url, verify_cmd=verify_cmd, open_pr=open_pr,
            done_when=done_when, backlog=backlog,
            skills_required=skills_required,
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
        if admission.warnings:
            # Keep the historical string-list shape so existing callers /
            # tests / dashboards don't break — warnings were already prose.
            result["warnings"] = [c.message for c in admission.warnings]
        return result

    def verify_goal(
        self, *, objective: str, workspace_dir: str,
        repo_url: Optional[str] = None, verify_cmd: Optional[str] = None,
        done_when: str = "", backlog: Optional[list[str]] = None,
        spec: str = "", skills_required: Optional[list[str]] = None,
    ) -> dict:
        """Pre-flight check the waiter calls before ``create_goal`` so the
        customer sees fixable conditions BEFORE thinking the order was filed.
        Same validations as ``create_goal`` runs internally; never mutates
        state; returns the structured :class:`AdmissionResult` as a dict."""
        from .admission import verify_goal as _verify

        return _verify(
            objective=objective, workspace_dir=workspace_dir, done_when=done_when,
            backlog=backlog, repo_url=repo_url, verify_cmd=verify_cmd, spec=spec,
            skills_required=skills_required,
        ).to_dict()

    def get_goal(self, goal_id: str) -> dict:
        if not self._goal_store.exists(goal_id):
            raise KeyError(goal_id)
        # Effective goal so the MCP-exposed view shows what cognition actually
        # uses (firmed-derived done_when, firmed stub_acceptable) — not the
        # original owner statement that's already been firmed past.
        # Display path: a corrupt firmed draft degrades to the base goal here
        # (the tick blocks the goal loudly; blocked_on carries the signal —
        # a dashboard read must never 500 over it).
        g = self._goal_store.load_effective_goal(goal_id, on_corrupt="none")
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
            "phase_history": [dict(e) for e in s.phase_history],
        }

    def _firmed_draft_payload(self, goal_id: str) -> Optional[dict]:
        """Serialize the goal's current firmed-draft for the waiter — only the
        fields the waiter renders (status, round, unknowns + their options/why,
        success_criteria). Returns None when firming hasn't run yet — and on a
        corrupt draft too (display path: the waiter's render must not raise;
        the tick blocks the goal loudly and blocked_on names the doc)."""
        draft = self._goal_store.read_firmed_draft(goal_id, on_corrupt="none")
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
        # Display path (see get_goal): corrupt firmed draft → base goal, no raise.
        g = self._goal_store.load_effective_goal(goal_id, on_corrupt="none")
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
        # Includes `workspace_dir`, `progress`, and `blocked_on` so
        # project_registry.project_rollup can derive project↔goal association
        # by workspace match — no stored goal_ids list to drift stale.
        out = []
        for gid in self._goal_store.list_goal_ids():
            g = self._goal_store.load_goal(gid)
            s = self._goal_store.load_status(gid)
            out.append({
                "id": gid,
                "objective": g.objective[:140],
                "workspace_dir": g.workspace_dir,
                "phase": s.phase,
                "lifecycle": s.lifecycle or "executing",
                "blocked_on": s.blocked_on,
                "progress": {"last_at": s.last_progress_at, "stalled": s.no_progress_notified},
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
        # stub_acceptable the owner added during firming. Cognition path, NOT
        # display: a corrupt firmed draft raises GoalDocCorrupt to the caller
        # — evaluating direction against the base goal would judge the wrong
        # contract, which is exactly the silent loss T0.4 closed.
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
        # Control path, NOT display: a corrupt draft raises GoalDocCorrupt
        # here — merging owner answers into a torn contract must fail loudly,
        # not read as "firming hasn't run" (the ValueError below).
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
        goal = self._goal_store.load_goal(goal_id)
        ctx = TickContext(
            store=self._goal_store, engine=self._engine,
            planner_caller=self._planner(), evaluator_caller=self._evaluator(),
            notifier=self._notifier, notify_url=self._cfg.notify_url,
            eval_every=self._cfg.eval_every, verify_done=self._verify_done(goal),
            autodeploy=self._autodeploy(goal),
            summary_caller=self._summary(), merger=self._merger(goal),
            remote_checker=self._remote_checker(),
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
