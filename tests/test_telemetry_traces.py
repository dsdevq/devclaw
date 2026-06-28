"""Telemetry / trace persistence tests.

Covers the three new seams:
  * StateStore traces table (append + read + aggregates)
  * PersistentTracer (in-memory + sqlite mirror)
  * Token estimate fields on CognitionEvent

Goal-layer integration (heartbeat creates a tracer per tick) is exercised in
test_goal_telemetry_integration to keep the units isolated.
"""

from __future__ import annotations

import tempfile

import pytest

from devclaw.loom import trace as _trace
from devclaw.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "telemetry.db"))


# ---- StateStore.append_trace_event / read_traces / trace_totals ------------


def test_append_and_read_trace_events_in_emission_order(store):
    store.append_trace_event(
        trace_id="t1", goal_id="g1", kind="cognition",
        payload={"role": "planner", "latency_ms": 120, "tokens_in_est": 50, "tokens_out_est": 10},
    )
    store.append_trace_event(
        trace_id="t1", goal_id="g1", kind="dispatch",
        payload={"tool": "implement_feature", "ref_id": "abc"},
    )
    rows = store.read_traces(goal_id="g1")
    assert len(rows) == 2
    assert [r["kind"] for r in rows] == ["cognition", "dispatch"]
    assert rows[0]["payload"]["role"] == "planner"
    assert rows[0]["trace_id"] == "t1"


def test_read_traces_isolates_by_goal_id(store):
    store.append_trace_event(trace_id="t1", goal_id="g1", kind="tick", payload={"phase": "idle"})
    store.append_trace_event(trace_id="t2", goal_id="g2", kind="tick", payload={"phase": "idle"})
    assert len(store.read_traces(goal_id="g1")) == 1
    assert len(store.read_traces(goal_id="g2")) == 1
    assert len(store.read_traces(goal_id="g3")) == 0


def test_read_traces_supports_since_id_cursor(store):
    ids = [
        store.append_trace_event(trace_id="t", goal_id="g", kind="note", payload={"i": i})
        for i in range(5)
    ]
    rows = store.read_traces(goal_id="g", since_id=ids[2])
    assert [r["payload"]["i"] for r in rows] == [3, 4]


def test_read_traces_filters_by_kind(store):
    store.append_trace_event(trace_id="t", goal_id="g", kind="cognition", payload={"role": "x"})
    store.append_trace_event(trace_id="t", goal_id="g", kind="dispatch", payload={"tool": "y"})
    store.append_trace_event(trace_id="t", goal_id="g", kind="cognition", payload={"role": "z"})
    cog = store.read_traces(goal_id="g", kind="cognition")
    assert len(cog) == 2
    assert all(r["kind"] == "cognition" for r in cog)


def test_trace_totals_aggregates_cognition_cost(store):
    store.append_trace_event(trace_id="t", goal_id="g", kind="cognition",
                             payload={"latency_ms": 100, "tokens_in_est": 1000, "tokens_out_est": 50})
    store.append_trace_event(trace_id="t", goal_id="g", kind="cognition",
                             payload={"latency_ms": 250, "tokens_in_est": 500, "tokens_out_est": 100})
    store.append_trace_event(trace_id="t", goal_id="g", kind="dispatch", payload={"tool": "x"})
    totals = store.trace_totals(goal_id="g")
    assert totals["events_by_kind"] == {"cognition": 2, "dispatch": 1}
    assert totals["cognition_total_latency_ms"] == 350
    assert totals["cognition_tokens_in_est"] == 1500
    assert totals["cognition_tokens_out_est"] == 150


def test_trace_totals_handles_no_data(store):
    totals = store.trace_totals(goal_id="never-existed")
    assert totals["events_by_kind"] == {}
    assert totals["cognition_total_latency_ms"] == 0


# ---- PersistentTracer ------------------------------------------------------


def test_persistent_tracer_mirrors_in_memory_and_sqlite(store):
    tracer = _trace.PersistentTracer(
        store=store, trace_id="trace-abc", goal_id="goal-1", label="test"
    )
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(
            role="planner", model="claude-sonnet-4-6",
            prompt="x" * 400, response="y" * 80, latency_ms=42,
        )
        _trace.record_tick(goal_id="goal-1", lifecycle="executing", phase="idle", outcome="advanced")

    # In-memory side
    assert len(tracer.events) == 2
    assert tracer.events[0]["kind"] == "cognition"
    # Persisted side
    rows = store.read_traces(goal_id="goal-1")
    assert len(rows) == 2
    assert rows[0]["trace_id"] == "trace-abc"
    assert rows[0]["payload"]["role"] == "planner"
    assert rows[0]["payload"]["latency_ms"] == 42
    # Token estimate populated from len(prompt)//4 + len(response)//4
    assert rows[0]["payload"]["tokens_in_est"] == 100  # 400/4
    assert rows[0]["payload"]["tokens_out_est"] == 20   # 80/4


def test_persistent_tracer_swallows_store_failures(store):
    # If the store write raises, the in-memory append still happens and the
    # cascade isn't broken — telemetry must not block production.
    class FailingStore:
        def append_trace_event(self, **kw):
            raise RuntimeError("simulated db failure")

    tracer = _trace.PersistentTracer(
        store=FailingStore(), trace_id="t", goal_id="g", label="test"
    )
    with _trace.tracer_scope(tracer):
        _trace.record_note("alive")
    assert len(tracer.events) == 1
    assert tracer.events[0]["text"] == "alive"


def test_no_tracer_means_no_persisted_rows(store):
    # Production invariant: production heartbeats opt into persistence; an
    # unattached call is silent. Tests that don't attach a tracer don't pollute.
    _trace.record_cognition(role="r", model="m", prompt="p", response="x", latency_ms=1)
    rows = store.read_traces(goal_id="anything")
    assert rows == []


# ---- tracer_scope context manager ------------------------------------------


def test_tracer_scope_restores_outer_tracer(store):
    outer = _trace.Tracer(label="outer")
    inner_persistent = _trace.PersistentTracer(
        store=store, trace_id="ti", goal_id="gi", label="inner"
    )
    with _trace.tracer_scope(outer):
        _trace.record_note("at-outer")
        with _trace.tracer_scope(inner_persistent):
            _trace.record_note("at-inner")
        _trace.record_note("back-at-outer")
    # outer saw two notes; inner saw one
    outer_texts = [e["text"] for e in outer.events]
    inner_texts = [e["text"] for e in inner_persistent.events]
    assert outer_texts == ["at-outer", "back-at-outer"]
    assert inner_texts == ["at-inner"]


def test_tracer_scope_none_is_safe_noop():
    with _trace.tracer_scope(None):
        _trace.record_note("would-be-dropped")
    # nothing should crash; no tracer attached, no record persisted
    assert _trace.get_tracer() is None


# ---- CognitionEvent token estimates ----------------------------------------


def test_cognition_event_estimates_tokens_from_text_length():
    tracer = _trace.Tracer()
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(
            role="x", model="m",
            prompt="a" * 1000, response="b" * 200, latency_ms=10,
        )
    e = tracer.events[0]
    assert e["tokens_in_est"] == 250   # 1000//4
    assert e["tokens_out_est"] == 50   # 200//4
