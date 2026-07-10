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


@pytest.fixture(autouse=True)
def _disable_sandbox_sweep_by_default(monkeypatch):
    """``TaskQueue.recover()`` sweeps orphaned sandbox containers via the real
    docker CLI. A test process is NOT the devclaw process: on a docker-enabled
    dev machine a live devclaw could be mid-task, and a test calling recover()
    must never ``docker rm -f`` its containers (the "any labeled container is
    orphaned" premise only holds for the real server's startup). Default the
    sweep to a no-op; the wiring test injects its own recording stub the same
    way, and the sweep's own unit tests patch its subprocess seam directly.
    """
    monkeypatch.setattr(task_queue, "sweep_orphan_sandboxes", lambda: 0)
