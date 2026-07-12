"""Unit tests for the delivery-strategy seam (PR1 — branch selection only)."""

import pytest

from devclaw.goal import delivery_strategy as ds
from devclaw.goal.models import Checklist, ChecklistItem


class _FakeStore:
    """Minimal stand-in exposing only ``read_checklist`` — records the kwargs it
    was called with so we can assert resolve_strategy keeps the fail-loud default.
    A callable ``checklist`` is invoked (so a test can raise)."""

    def __init__(self, checklist):
        self._checklist = checklist
        self.calls: list[dict] = []

    def read_checklist(self, goal_id, *, on_corrupt="raise"):
        self.calls.append({"goal_id": goal_id, "on_corrupt": on_corrupt})
        if isinstance(self._checklist, Exception):
            raise self._checklist
        return self._checklist


def test_checklist_present_resolves_goal_branch():
    store = _FakeStore(Checklist(items=[
        ChecklistItem(id="1", requirement="do a thing", evidence_target="thing.py")
    ]))
    strat = ds.resolve_strategy(store, "g1")
    assert strat is ds.GOAL_BRANCH
    assert strat.goal_branch("g1") == "goal/g1"


def test_no_checklist_resolves_per_action():
    store = _FakeStore(None)
    strat = ds.resolve_strategy(store, "g1")
    assert strat is ds.PER_ACTION
    assert strat.goal_branch("g1") is None


def test_empty_checklist_still_goal_branch():
    # Checklist is a plain frozen dataclass — always truthy even with no items —
    # so the pre-refactor split between `is not None` (remote-checks site) and
    # truthiness (dispatch/done-gate sites) collapses to one predicate. This test
    # pins that equivalence: an item-less checklist must still be goal-branch.
    store = _FakeStore(Checklist(items=[]))
    assert ds.resolve_strategy(store, "g1") is ds.GOAL_BRANCH


def test_resolve_keeps_fail_loud_default_and_propagates_corruption():
    # resolve_strategy must NOT pass on_corrupt="none" — it inherits the call
    # sites' default fail-loud behaviour (T0.4), so a corrupt contract raises
    # here rather than being silently downgraded to per-action.
    boom = RuntimeError("corrupt contract")
    store = _FakeStore(boom)
    with pytest.raises(RuntimeError, match="corrupt contract"):
        ds.resolve_strategy(store, "g1")
    assert store.calls == [{"goal_id": "g1", "on_corrupt": "raise"}]
