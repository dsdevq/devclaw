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

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

# The engine literal stays a Literal for forward-compat (a future content engine
# would extend it), but code is the only engine today and dispatch is in-process.
Engine = Literal["devclaw"]
#: the engine verbs the goal layer can dispatch — a subset of devclaw's task
#: kinds plus the program decomposer.
GoalTool = Literal["start_program", "implement_feature", "fix_bug", "review_repository"]
Phase = Literal["idle", "in_flight", "verifying", "blocked", "done", "cancelled"]
#: The OUTCOME lifecycle — a goal stated as an outcome investigates the repo
#: (research → discovery brief) before it executes, so devclaw behaves like a
#: senior dev handed an outcome by a non-technical owner. Distinct from ``Phase``
#: (the per-tick execution state): ``Lifecycle`` is the coarse stage of the goal.
#: ``None`` on a stored status means a legacy goal created before the lifecycle
#: existed — treated as ``executing`` so it keeps running the flat backlog.
Lifecycle = Literal["investigating", "firming", "executing"]
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
    #: explicit list of MCP tool names (or capability slugs) for which a
    #: ``not_yet_available`` stub is an acceptable terminal state. The
    #: decomposer is forbidden from emitting stubs unless the tool appears
    #: here; the done-gate refuses to mark a stub-shaped clause satisfied
    #: unless one of these names appears in the clause/evidence text. Empty
    #: list (the default) means "no stubs allowed — plan real work."
    stub_acceptable: list[str] = field(default_factory=list)
    #: slugs of skills from the host's skill library (``devclaw.skill_library``)
    #: that should be provisioned into ``<workspace>/.agent/skills/`` before the
    #: executor runs against this goal. Per-project tech-stack briefings the
    #: agent loads on each task. Empty list (default) = no extras beyond the
    #: universal devclaw skill bundle + any repo-committed ``.agent/skills/``
    #: contents. Admission validates each slug exists in the library.
    skills_required: list[str] = field(default_factory=list)


#: case-insensitive markers by which a ``done_when`` disclaims boundedness —
#: the owner is saying "this goal has no terminal completion state; judge each
#: delivery, don't ever close it". The closeloop-bench-2026-07-05 contract read
#: "Not applicable as a bounded criterion — this is a standing goal" and the
#: done-gate still returned a terminal ``achieved``; :func:`is_standing` is how
#: the evaluator honors that wording instead of overriding it. Deliberately a
#: short, conservative list: a false positive merely routes the close decision
#: to the owner (needs_human), a false negative reproduces the benchmark bug —
#: extend it when a real contract phrasing slips through, not speculatively.
_STANDING_DONE_WHEN = re.compile(
    r"standing goal"
    r"|not a bounded criterion"
    r"|not applicable as a bounded"
    r"|no terminal (?:state|completion)",
    re.IGNORECASE,
)


def is_standing(done_when: str) -> bool:
    """True when ``done_when`` declares the goal standing (unbounded). Such a
    goal must never terminally close via the done-gate — completion is the
    owner's call (``cancel_goal`` / re-aim), so an all-clauses-pass done-gate
    verdict becomes ``needs_human`` instead of ``achieved``."""
    return bool(done_when and _STANDING_DONE_WHEN.search(done_when))


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
    #: checklist item ids this in-flight action serves (Pillar 1). The settle
    #: hook reads these back and updates the checklist (status + evidence) on
    #: terminal poll. Empty in legacy backlog-mode goals.
    addresses: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GoalStatus:
    """Mutable per-tick state — STATUS.md frontmatter. Overwritten, never appended."""

    phase: Phase = "idle"
    #: the outcome lifecycle stage (None = legacy goal → behaves as "executing")
    lifecycle: Optional[Lifecycle] = None
    in_flight: Optional[InFlight] = None
    blocked_on: Optional[str] = None
    #: structured classification of the CURRENT block — the machine-readable
    #: sibling of the human-readable ``blocked_on`` prose (a planned auto-heal
    #: pass must never string-match ``blocked_on`` to decide what it may retry).
    #: Taxonomy: ``mechanical:<site>`` (the condition is cheaply re-checkable
    #: without an LLM — ``mechanical:prep`` / ``mechanical:corrupt_doc`` /
    #: ``mechanical:lost_ref`` / ``mechanical:dispatch_cap``); ``needs_answer``
    #: (cognition asked the owner a question); ``bug`` (the force_block
    #: illegal-transition escape hatch). ``""`` = not blocked, or a block that
    #: predates this field / wasn't classified. Only meaningful while
    #: ``phase == "blocked"`` — the store clears it on any write that lands on
    #: a non-blocked phase (see GoalStatusMixin._normalized_blocked_kind).
    blocked_kind: str = ""
    #: auto-heal damping counter (F8): how many times the tick's mechanical
    #: auto-heal has lifted a ``mechanical:*`` block for this goal since a
    #: human last vouched for it. Persisted — a flapping condition
    #: (block → heal → re-block) must not turn the zero-token blocked
    #: steady-state into an LLM call per cycle, so the heal refuses past a
    #: small cap and hands the goal back to the owner (one plain ping,
    #: marked by bumping this one past the cap). Reset to 0 when a HUMAN
    #: lifts a block (steer_goal) and on a productive settle (the same
    #: stability signal that refunds the dispatch cap).
    heal_attempts: int = 0
    #: human note of the intended next step
    next: str = ""
    #: ISO ts of the last time the plan step (LLM) ran
    last_plan_at: Optional[str] = None
    #: ISO ts of the last tick (cheap or not)
    last_tick_at: Optional[str] = None
    #: the INGEST boundary, not a consume cursor: number of inbox.md lines
    #: already turned into goal_steering rows (Tranche 1/PR5 repurposed this
    #: field — pre-PR5 it WAS the consume cursor; since PR5, consumption of
    #: STEERING is by exact row id via
    #: GoalStore.transition(consume_steering=...), never by counting lines —
    #: see GoalStore._ingest_inbox). Load-bearing, not a deprecated leftover:
    #: PR8 confirmed it stays (it predates PR5 on the roadmap as "delete
    #: this", but PR5 gave it this new, still-in-use job). Also carried on
    #: STATUS.md frontmatter for rendering / rollback fidelity.
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
    #: Append-only trail of phase transitions — one dict per entry-to-a-new-phase
    #: (``{"phase": str, "at": iso_ts}``). Written by the store on save_status
    #: whenever the phase changes; read by the console for the timeline
    #: timestamps. Deliberately unbounded — the log is human-scale (dozens of
    #: entries per goal at most).
    phase_history: tuple[dict, ...] = ()
    #: the stored State value (see devclaw.goal.transitions) — None on a
    #: legacy row / a status object never round-tripped through the store.
    #: compare=False: two GoalStatus objects with identical business fields
    #: still compare equal regardless of this projection (existing tests
    #: build expected GoalStatus(...) objects without ever setting it — see
    #: tests/test_goal_status_migration.py's `migrated == GoalStatus()`).
    state: Optional[str] = field(default=None, compare=False)
    #: optimistic-concurrency counter GoalStore.transition() CAS's against —
    #: bumped by exactly 1 on every store write (save_status / transition /
    #: update_status_fields). compare=False for the same reason as `state`.
    version: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Action:
    """One engine call the planner chose. ``addresses`` carries the checklist
    item ids this action serves when the goal is in checklist-mode (Pillar 1) —
    the dispatch hook flips those items to ``in_flight`` and the settle hook
    fills their ``evidence`` + flips them to ``done`` on success. Empty for
    legacy backlog-mode goals."""

    engine: Engine
    tool: GoalTool
    goal: str
    verify_cmd: Optional[str] = None
    open_pr: bool = True
    addresses: list[str] = field(default_factory=list)
    #: A concise conventional-commit-shaped PR title the PLANNER chose based on
    #: what it's asking the executor to build — e.g. ``feat: add /health
    #: endpoint``. Threaded planner → Task → delivery so the opened PR reads as
    #: what was asked, not what a summary of a mid-work commit subject
    #: interpreted after the fact (see plan.md §Production-ready C7 and commit
    #: d41d27b which grounded the fallback but couldn't remove the guesswork).
    #: Optional: when None, delivery falls back to the engineer's own commit
    #: subject, then the diff-grounded _pr_title(goal, kind) heuristic.
    title: Optional[str] = None
    #: True when the checklist item(s) this action serves are all generated
    #: scaffolding (see :attr:`ChecklistItem.scaffold`). DERIVED mechanically at
    #: dispatch from the addressed items (not chosen by the per-tick planner LLM),
    #: then threaded onto the task row so the queue skips the adversarial review
    #: gate for it. Default False. SAFETY: skips review ONLY — the verify gate +
    #: test-integrity still run (enforced in task_queue._run_and_settle).
    scaffold: bool = False


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


#: An item's lifecycle inside the checklist. ``not_started`` is the
#: planner's pick-pool; ``in_flight`` is dispatched and not yet settled;
#: ``done`` is verified with non-null evidence; ``blocked`` waits on a human
#: decision; ``mis_specified`` is the executor's "this item doesn't match the
#: code I'm seeing" signal — surfaces as a steer event for the owner.
ItemStatus = Literal["not_started", "in_flight", "done", "blocked", "mis_specified"]
#: model-tier hint per item (defaults to the global executor tier when absent)
ItemModelTier = Literal["haiku", "sonnet", "opus"]


@dataclass(frozen=True)
class ChecklistItem:
    """One atomic unit of work the decomposer emitted. Each item is one
    focused commit's worth — small enough that ONE agent finishes it in one
    sandbox cycle. The whole point is per-item verifiability: the `evidence`
    string is what the gate confirms exists in the diff/repo before flipping
    `status` to `done`. See ``devclaw/prompts/decomposer.md`` for the
    schema contract this matches."""

    id: str
    requirement: str
    evidence_target: str
    addresses_files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: ItemStatus = "not_started"
    #: filled by the executor on settle — concrete proof the item was met
    #: (file:line + symbol/test names). Null until the gate verifies the diff.
    evidence: Optional[str] = None
    #: rough focused-agent-time estimate; the scheduler reads this to budget
    #: per-tick dispatch. None → scheduler picks a default.
    effort_minutes: Optional[int] = None
    #: per-item model tier hint; None → the global executor tier
    #: (DEVCLAW_EXEC_MODEL) is used.
    model_tier: Optional[ItemModelTier] = None
    #: free-form one-liner from the decomposer for the executor; prefix
    #: ``legit_stub: `` marks the item as a deliberate not_yet_available
    #: stub rather than work-to-do.
    note: str = ""
    #: the milestone (phase) this item rolls up to, matching one of the
    #: heading strings under the spec's ``## Milestones`` section. Lets the
    #: planner pick a coherent set of next items, the dashboard render
    #: milestone-grouped progress, and the evaluator judge milestone-level
    #: completion. ``None`` is valid — small checklists may omit milestones
    #: entirely, and legacy decomposer output that pre-dated this field
    #: still parses cleanly without it.
    milestone: Optional[str] = None
    #: True when this item is *generated scaffolding* — a boilerplate-setup step
    #: whose diff is generator output (``ng new``, ``dotnet new``, a workspace /
    #: test-project skeleton), not hand-authored logic. The decomposer tags it
    #: (L3, issue #222) so the dispatch path can skip the ADVERSARIAL CODE-REVIEW
    #: gate for it — an oversized generated diff crashes that reviewer and, more
    #: fundamentally, scaffolding is a different operation than implementing logic
    #: and is verified STRUCTURALLY (does it build?) not by reading the diff.
    #: SAFETY: this flag ONLY skips adversarial review. A scaffold item MUST still
    #: pass the verify_cmd/build gate + the test-integrity scan, so an over-tagged
    #: real code task is at worst "unreviewed but still must build + pass tests" —
    #: never "ships broken or untested." Tag CONSERVATIVELY: only clear generator-
    #: output steps. Default False = a normal, fully-reviewed item.
    scaffold: bool = False


@dataclass(frozen=True)
class Checklist:
    """The full decomposer output for one goal — the durable structured plan
    the planner picks actions from, the gate verifies against, and the owner
    edits via steer. Stored under ``<goal_id>/checklist.yaml`` alongside
    ``STATUS.md`` etc., mutable across ticks."""

    items: list[ChecklistItem] = field(default_factory=list)
    #: questions for the owner the decomposer couldn't decide from the digest
    open_questions: list[str] = field(default_factory=list)
    #: free-form observations for the per-tick planner (file overlaps,
    #: conditional outcomes the executor must resolve, etc.)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClauseVerdict:
    """One atomic ``done_when`` clause + the evaluator's per-clause finding.

    At the done-gate the evaluator decomposes ``done_when`` into independent
    requirements joined by AND and grades each one against the repo-review
    evidence. The aggregate verdict on the parent :class:`EvalResult` is then
    derived from these: ``achieved`` requires every clause to be satisfied with
    non-empty evidence; any unsatisfied clause forces ``off_track`` with that
    clause cited in the corrections (closes the 2026-06-25 "stub everything"
    failure mode)."""

    clause: str
    satisfied: bool
    #: file path(s) + symbol/test name(s) confirming satisfaction, OR an explicit
    #: "missing — should live in <where>" note when unsatisfied. Vague prose is
    #: rejected by the evaluator prompt; a non-empty string here is the evidence
    #: contract.
    evidence: str = ""


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
    #: per-clause findings — populated at the done-gate. Empty pre-done-gate.
    clauses: list[ClauseVerdict] = field(default_factory=list)
    #: axis-B verdict at the done-gate: ``clean`` | ``concerns`` | ``poor``.
    #: Empty pre-done-gate. When ``poor`` (or ``concerns`` with substantive named
    #: items), ``validate()`` mechanically downgrades ``achieved`` → ``off_track``
    #: — the second half of the closeloop-D1/D2/D6 safety net (the model has an
    #: incentive to declare done; mechanism has none).
    structural_health: str = ""
    #: itemized structural concerns backing ``structural_health``. Empty when
    #: ``clean``. When ``poor`` or a substantive ``concerns``, each entry becomes
    #: a correction on the downgrade path.
    structural_concerns: list[str] = field(default_factory=list)


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
