"""Delivery strategy — how a goal's task work maps onto git branches + PRs.

This NAMES a decision that already existed implicitly, smeared across the tick
and delivery layers as ad-hoc ``store.read_checklist(...)`` / ``f"goal/{id}"``
conditionals: a checklist-mode goal accumulates every item's commits on a shared
``goal/<id>`` branch (one cumulative PR per goal — Pillar 1); a legacy /
non-checklist goal delivers each action as its own branch + PR off the default
branch.

It is the seam a second topology (per-task PRs to main) plugs into later, instead
of threading a new conditional through every call site. TODAY it owns ONLY the
branch-selection decision — the one part that extracts with provably zero
behaviour change: ``Checklist`` is a plain frozen dataclass (always truthy, no
``__bool__``/``__len__``), so the pre-existing ``is not None`` vs. truthiness
split across the three call sites collapses to a single predicate.

Auto-merge eligibility (the tick still keys off ``bool(addresses)``) and the
scheduler dep-gate stay at their call sites on purpose: they carry latent
per-action-vs-per-goal signal mismatches that must be reconciled *deliberately*
in the PR that actually changes their behaviour — not laundered through a
"pure refactor".

Resolution mirrors :func:`devclaw.goal.merge.resolve_automerge`: a pure function
of goal state, trivially unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from .store import GoalStore


class DeliveryStrategy(Protocol):
    """How one goal's task work maps to branches / PRs."""

    #: stable identifier for logs / telemetry
    name: str

    def goal_branch(self, goal_id: str) -> Optional[str]:
        """The shared branch a goal's writes accumulate on (delivery reuses that
        branch's single PR across items), or ``None`` when each action delivers
        its own branch + PR off the default branch.

        The read-only ``review_repository`` exclusion is deliberately NOT here:
        that's an action-level concern (a read-only action never writes, so it
        runs on the default branch whatever the strategy) applied by the caller.
        """
        ...


class GoalBranchStrategy:
    """Every item's commits stack on ``goal/<id>``; one cumulative PR per goal.
    Today's behaviour for checklist-mode goals (Pillar 1)."""

    name = "goal-branch"

    def goal_branch(self, goal_id: str) -> Optional[str]:
        return f"goal/{goal_id}"


class PerActionStrategy:
    """Each action delivers its own branch + PR off the default branch; no shared
    goal branch. Today's behaviour for legacy / non-checklist goals."""

    name = "per-action"

    def goal_branch(self, goal_id: str) -> Optional[str]:
        return None


#: stateless singletons — the strategies carry no per-goal state
GOAL_BRANCH: "DeliveryStrategy" = GoalBranchStrategy()
PER_ACTION: "DeliveryStrategy" = PerActionStrategy()


def resolve_strategy(store: "GoalStore", goal_id: str) -> "DeliveryStrategy":
    """The delivery strategy for a goal: ``goal-branch`` once the decomposer has
    produced a checklist (items accumulate on one branch), else ``per-action``.

    Reproduces EXACTLY the ``store.read_checklist(goal_id) is not None`` gate that
    guarded the inline ``f"goal/{goal_id}"`` computations before extraction — and
    keeps their default ``on_corrupt="raise"`` semantics, so a corrupt contract
    still fails loud here rather than being silently downgraded to per-action.
    """
    if store.read_checklist(goal_id) is not None:
        return GOAL_BRANCH
    return PER_ACTION
