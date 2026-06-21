"""Shared test fixtures + hermeticity guards."""

import pytest

from devclaw import task_queue


@pytest.fixture(autouse=True)
def _disable_review_gate_by_default(monkeypatch):
    """The pre-PR review gate's default reviewer shells out to the real `claude`
    CLI. On a developer machine that's authenticated, an un-injected TaskQueue in
    a test with a real git workspace would make a live, non-deterministic Claude
    call (and in CI it would just fail open). Keep the whole suite hermetic by
    defaulting the gate OFF; the review-gate tests re-enable it explicitly and
    inject a stub reviewer.
    """
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", False)
