"""Cognition seam — the one chokepoint behind which the LLM hides."""

from __future__ import annotations

import os

import pytest

from devclaw import cognition
from devclaw.cognition import (
    ClaudeCognition,
    Cognition,
    StubCognition,
    bind,
    get_cognition,
    set_cognition,
)


@pytest.fixture(autouse=True)
def _reset_default():
    """Each test starts with no cached default."""
    set_cognition(None)
    yield
    set_cognition(None)


def test_env_default_is_claude(monkeypatch):
    monkeypatch.delenv("DEVCLAW_COGNITION", raising=False)
    cog = get_cognition()
    assert isinstance(cog, ClaudeCognition)


def test_env_stub_selects_stub(monkeypatch):
    monkeypatch.setenv("DEVCLAW_COGNITION", "stub")
    cog = get_cognition()
    assert isinstance(cog, StubCognition)


def test_env_unknown_value_raises(monkeypatch):
    monkeypatch.setenv("DEVCLAW_COGNITION", "openai")
    with pytest.raises(ValueError, match="unknown DEVCLAW_COGNITION"):
        get_cognition()


@pytest.mark.asyncio
async def test_stub_returns_canned_response_by_role():
    cog = StubCognition(
        responses={"planner": "PLAN", "evaluator": "EVAL"},
        default="DEFAULT",
    )
    assert await cog("p", role="planner") == "PLAN"
    assert await cog("p", role="evaluator") == "EVAL"
    assert await cog("p", role="grill") == "DEFAULT"  # falls back
    assert len(cog.calls) == 3
    assert cog.calls[0] == ("planner", "", "p")


@pytest.mark.asyncio
async def test_bind_routes_through_configured_cognition():
    stub = StubCognition(responses={"planner": "OK"})
    set_cognition(stub)
    caller = bind("opus", role="planner")
    out = await caller("plan this")
    assert out == "OK"
    # the call carried the bound model + role through to the cognition
    assert stub.calls[-1] == ("planner", "opus", "plan this")


@pytest.mark.asyncio
async def test_swap_cognition_at_runtime():
    """A test harness can swap the default mid-process."""
    set_cognition(StubCognition(default="first"))
    caller = bind(None, role="planner")
    assert await caller("p") == "first"
    set_cognition(StubCognition(default="second"))
    # caller bound to bind() reads get_cognition() each call → picks up the swap
    assert await caller("p") == "second"


def test_set_none_resets_to_lazy_from_env(monkeypatch):
    monkeypatch.setenv("DEVCLAW_COGNITION", "stub")
    set_cognition(StubCognition(default="explicit"))
    assert isinstance(get_cognition(), StubCognition)
    set_cognition(None)
    # next get_cognition rebuilds from env
    cog = get_cognition()
    assert isinstance(cog, StubCognition)


@pytest.mark.asyncio
async def test_stub_records_into_trace():
    """The stub backend writes the same cognition events the live backend
    would, so a harness running under stub produces the same trace shape as
    one running live."""
    from devclaw.loom.trace import Tracer, set_tracer

    set_cognition(StubCognition(responses={"planner": "{}"}))
    tracer = Tracer(label="t")
    set_tracer(tracer)
    try:
        await bind("opus", role="planner")("hello")
    finally:
        set_tracer(None)
    cog_events = tracer.by_kind("cognition")
    assert len(cog_events) == 1
    assert cog_events[0]["role"] == "planner"
    assert cog_events[0]["model"] == "opus"
