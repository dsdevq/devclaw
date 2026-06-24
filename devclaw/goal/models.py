"""Goal-layer domain types — the durable mind, as plain data.

Folded in from goalclaw. A :class:`Goal` is the durable objective (read from
``goal.yaml``); a :class:`GoalStatus` is the mutable point-in-time state
(``STATUS.md`` frontmatter), overwritten each tick. An :class:`Action` is a
single engine call the planner decided on; a :class:`PlanResult` is the whole
next-action decision. :class:`EvalResult` is the direction evaluator's verdict —
the layer that asks "is this going the right way?" not just "did it ship?".

These are deliberately separate from the task/program types in
``state_store.py``: a ``program`` is a bounded, one-shot DAG that runs to
completion; a ``goal`` is an open-ended standing intent advanced across days via
the heartbeat + steering. Different time-scales, different lifecycles — the goal
layer sits *above* the program/task engine and dispatches into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# The engine literal stays a Literal for forward-compat (a future content engine
# would extend it), but code is the only engine today and dispatch is in-process.
Engine = Literal["devclaw"]
#: the engine verbs the goal layer can dispatch — a subset of devclaw's task
#: kinds plus the program decomposer.
GoalTool = Literal["start_program", "implement_feature", "fix_bug", "review_repository"]
Phase = Literal["idle", "in_flight", "verifying", "blocked", "done", "cancelled"]
#: The OUTCOME lifecycle — a goal stated as an outcome grows a planning front-end
#: (research → align → plan) before it executes, so devclaw behaves like a senior
#: dev handed an outcome by a non-technical owner. Distinct from ``Phase`` (the
#: per-tick execution state): ``Lifecycle`` is the coarse stage of the whole goal.
#: ``None`` on a stored status means a legacy goal created before the lifecycle
#: existed — treated as ``executing`` so it keeps running the flat backlog.
Lifecycle = Literal[
    "new", "investigating", "grilling", "plan_review", "executing", "verifying", "done"
]
Decision = Literal["act", "sleep", "blocked", "done"]
EvalVerdict = Literal["on_track", "off_track", "achieved", "stalled", "needs_human"]


@dataclass(frozen=True)
class Goal:
    """The durable objective. Read from ``<goal_id>/goal.yaml``; treated as facts."""

    id: str
    objective: str
    #: heartbeat cadence to re-plan even with no event, e.g. "6h", "1d"
    cadence: str
    engine: Engine
    workspace_dir: str
    #: git URL of the target repo — the goal layer clones it if workspace_dir is
    #: empty, and resets to its default branch before each action. None → must pre-exist.
    repo_url: Optional[str] = None
    #: gate command devclaw runs after the agent ("the agent's done is not trusted")
    verify_cmd: Optional[str] = None
    #: when True, devclaw delivers each change as a PR to review
    open_pr: bool = True
    #: prose statement of completion, evaluated by the direction evaluator
    done_when: str = ""
    #: concrete starting work-list the planner draws the next action from
    backlog: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InFlight:
    """A reference to an action the engine is currently running for this goal."""

    engine: Engine
    tool: GoalTool
    #: the task_id or program_id the engine returned
    id: str
    #: "task" | "program" — which kind of row to poll
    ref_kind: Literal["task", "program"]
    goal: str = ""
    #: True when this is the read-only review dispatched by the done-gate (its
    #: terminal result feeds the evaluator, not the next-action planner).
    is_done_check: bool = False
    #: True when this is the read-only repo analysis dispatched by the
    #: ``investigating`` lifecycle phase (its terminal result feeds the discovery
    #: synthesis, not the planner or the done-gate evaluator).
    is_discovery: bool = False


@dataclass(frozen=True)
class GoalStatus:
    """Mutable per-tick state — STATUS.md frontmatter. Overwritten, never appended."""

    phase: Phase = "idle"
    #: the outcome lifecycle stage (None = legacy goal → behaves as "executing")
    lifecycle: Optional[Lifecycle] = None
    in_flight: Optional[InFlight] = None
    blocked_on: Optional[str] = None
    #: human note of the intended next step
    next: str = ""
    #: ISO ts of the last time the plan step (LLM) ran
    last_plan_at: Optional[str] = None
    #: ISO ts of the last tick (cheap or not)
    last_tick_at: Optional[str] = None
    #: number of inbox.md lines already consumed as steering
    inbox_cursor: int = 0
    #: total engine actions dispatched for this goal — a runaway backstop
    actions_dispatched: int = 0
    #: delivered actions since the last direction evaluation — drives eval cadence
    deliveries_since_eval: int = 0
    #: the last direction-eval verdict + when, surfaced via get_goal (observe surface)
    last_eval_verdict: Optional[EvalVerdict] = None
    last_eval_at: Optional[str] = None
    last_eval_note: str = ""
    #: ISO ts of the last forward progress — a delivery, or (self-initialized by the
    #: watchdog) when the goal first entered executing. The no-progress watchdog
    #: measures wall-clock from here; reset on every delivery. None until executing.
    last_progress_at: Optional[str] = None
    #: True once the no-progress watchdog has pinged the owner for the CURRENT stall;
    #: cleared on the next delivery so a later stall fires again (ping once per stall).
    no_progress_notified: bool = False


@dataclass(frozen=True)
class Action:
    """One engine call the planner chose."""

    engine: Engine
    tool: GoalTool
    goal: str
    verify_cmd: Optional[str] = None
    open_pr: bool = True


@dataclass(frozen=True)
class PlanResult:
    """The next-action planner's full decision for one wakeup."""

    decision: Decision
    #: present when decision == "act"
    actions: list[Action] = field(default_factory=list)
    #: present when decision == "blocked"
    question: str = ""
    #: human-readable summary for the log + notify, any decision
    note: str = ""


@dataclass(frozen=True)
class EvalResult:
    """The direction evaluator's verdict — grounded in delivered artifacts, not
    in backlog-counts. ``verdict`` drives the loop: ``achieved`` closes the goal;
    ``off_track`` writes ``corrections`` into the inbox as steering; ``stalled``
    and ``needs_human`` block; ``on_track`` just records and continues."""

    verdict: EvalVerdict
    rationale: str = ""
    #: concrete corrections / new direction the evaluator wants pursued — written
    #: to inbox.md as steering so the next-action planner honors them.
    corrections: list[str] = field(default_factory=list)
    #: present when verdict == "needs_human"
    question: str = ""


@dataclass(frozen=True)
class PollResult:
    """Outcome of polling an in-flight engine ref."""

    terminal: bool
    #: pending | running | done | failed | cancelled | planning | ...
    status: str
    #: the engine's full result/error detail, surfaced to cognition on terminal
    detail: str = ""
    #: delivery evidence — the PR url the engine opened (None if not delivered)
    pr_url: Optional[str] = None
    #: verify-gate verdict (None if no gate ran)
    gate_passed: Optional[bool] = None

    @property
    def running(self) -> bool:
        return not self.terminal
