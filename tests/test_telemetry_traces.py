"""Telemetry / trace persistence tests.

Covers the three new seams:
  * StateStore traces table (append + read + aggregates)
  * PersistentTracer (in-memory + sqlite mirror)
  * Token estimate fields on CognitionEvent

Goal-layer integration (heartbeat creates a tracer per tick) is exercised in
test_goal_telemetry_integration to keep the units isolated.
"""

from __future__ import annotations

import json
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
    # T0.5: no envelope passed → real-usage fields explicitly absent (None)
    assert e["tokens_in"] is None and e["tokens_out"] is None
    assert e["cost_usd"] is None


# ---- T0.5: real usage + full response text -----------------------------------


def test_record_cognition_carries_real_usage_and_full_response(store):
    tracer = _trace.PersistentTracer(store=store, trace_id="t", goal_id="g", label="x")
    long_response = json.dumps({"filler": "z" * 400, "verdict": "achieved"})
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(
            role="evaluator", model="sonnet",
            prompt="p" * 500, response=long_response, latency_ms=7,
            tokens_in=1234, tokens_out=56, cache_read=999, cache_creation=111,
            cost_usd=0.0123,
        )
    (row,) = store.read_traces(goal_id="g")
    p = row["payload"]
    # full response persisted (past the 240-char preview horizon)
    assert p["response_text"] == long_response
    assert len(p["response_preview"]) <= 240
    # real usage fields
    assert p["tokens_in"] == 1234
    assert p["tokens_out"] == 56
    assert p["cache_read"] == 999
    assert p["cache_creation"] == 111
    assert p["cost_usd"] == pytest.approx(0.0123)
    # est fields still present alongside
    assert p["tokens_in_est"] == 125  # 500//4
    # the full PROMPT must NOT be in the row (can exceed 128KB)
    assert "p" * 500 not in json.dumps(p)


# ---- T0.5: goal-scoped transcripts -------------------------------------------


def test_goal_scoped_cognition_writes_transcript_file(store, tmp_path):
    goals_dir = tmp_path / "goals"
    tracer = _trace.PersistentTracer(
        store=store, trace_id="t1", goal_id="goal-x", label="tick",
        goals_dir=goals_dir,
    )
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(
            role="planner", model="opus",
            prompt="THE FULL PROMPT " * 100, response='{"tasks": []}',
            latency_ms=5, tokens_in=42, tokens_out=7, cost_usd=0.005,
        )
    (row,) = store.read_traces(goal_id="goal-x", kind="cognition")
    fname = row["payload"]["transcript_file"]
    assert fname and fname.endswith("-planner.md")
    tfile = goals_dir / "goal-x" / "transcripts" / fname
    assert tfile.is_file()
    text = tfile.read_text()
    # header: role, model, tokens, cost — then full prompt, then full response
    assert "- role: planner" in text
    assert "- model: opus" in text
    assert "- tokens_in: 42" in text
    assert "- tokens_out: 7" in text
    assert "- cost_usd: 0.005000" in text
    assert "## prompt" in text and ("THE FULL PROMPT " * 100) in text
    assert "## response" in text and '{"tasks": []}' in text
    # ...while the sqlite row does NOT carry the full prompt — only the
    # 240-char preview (the full text lives in the transcript file above)
    assert ("THE FULL PROMPT " * 100) not in json.dumps(row["payload"])
    assert len(row["payload"]["prompt_preview"]) <= 240


def test_transcript_estimated_tokens_labeled_when_no_envelope(store, tmp_path):
    goals_dir = tmp_path / "goals"
    tracer = _trace.PersistentTracer(
        store=store, trace_id="t", goal_id="g", goals_dir=goals_dir,
    )
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="evaluator", model="sonnet", prompt="x" * 40, response="y" * 8)
    fname = tracer.events[0]["transcript_file"]
    text = (goals_dir / "g" / "transcripts" / fname).read_text()
    assert "- tokens_in: ~10 (est)" in text
    assert "- tokens_out: ~2 (est)" in text
    assert "- cost_usd: n/a" in text


def test_transcript_filenames_unique_within_same_millisecond(store, tmp_path):
    goals_dir = tmp_path / "goals"
    tracer = _trace.PersistentTracer(
        store=store, trace_id="t", goal_id="g", goals_dir=goals_dir,
    )
    with _trace.tracer_scope(tracer):
        for _ in range(3):
            _trace.record_cognition(role="planner", model="m", prompt="p", response="r")
    names = [e["transcript_file"] for e in tracer.events]
    assert len(set(names)) == 3
    for n in names:
        assert (goals_dir / "g" / "transcripts" / n).is_file()


def test_no_goals_dir_means_no_transcript(store, tmp_path):
    # A PersistentTracer WITHOUT goals_dir (pre-T0.5 construction) records the
    # event but writes no file — non-goal-scoped cognition is unchanged.
    tracer = _trace.PersistentTracer(store=store, trace_id="t", goal_id="g")
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="planner", model="m", prompt="p", response="r")
    assert tracer.events[0]["transcript_file"] == ""
    assert not list(tmp_path.rglob("transcripts"))


def test_plain_tracer_never_writes_transcripts(tmp_path):
    tracer = _trace.Tracer(label="in-memory")
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="planner", model="m", prompt="p", response="r")
    assert tracer.events[0]["transcript_file"] == ""


def test_transcript_write_failure_is_swallowed(store, tmp_path):
    # goals_dir path occupied by a FILE → mkdir raises → transcript silently
    # skipped, event still recorded (telemetry must never break the cascade).
    not_a_dir = tmp_path / "goals"
    not_a_dir.write_text("i am a file")
    tracer = _trace.PersistentTracer(
        store=store, trace_id="t", goal_id="g", goals_dir=not_a_dir,
    )
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="planner", model="m", prompt="p", response="r")
    assert len(tracer.events) == 1
    assert tracer.events[0]["transcript_file"] == ""


def test_goal_service_make_tracer_binds_goals_dir(store, tmp_path, monkeypatch):
    """GoalService plumbs its configured goals_dir into the per-tick tracer, so
    goal-scoped cognition transcripts land under <goals_dir>/<goal_id>/transcripts/."""
    from devclaw.goal import service as service_mod
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.task_queue import TaskQueue

    monkeypatch.setattr(service_mod, "_TRACE_PERSIST_ENABLED", True)
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir, notify_url="", tick_seconds=900,
        eval_every=3, verify_done=False,
    )
    svc = GoalService(TaskQueue(store), store, config=cfg)
    tracer = svc._make_tracer("g1")
    assert isinstance(tracer, _trace.PersistentTracer)
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="planner", model="m", prompt="p", response="r")
    fname = tracer.events[0]["transcript_file"]
    assert fname
    assert (goals_dir / "g1" / "transcripts" / fname).is_file()


# ---- T0.5: trace_totals prefers real usage -----------------------------------


def test_trace_totals_prefers_real_tokens_and_sums_cost(store):
    # one row with real usage (post-T0.5), one legacy row with only estimates
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="cognition",
        payload={"latency_ms": 100, "tokens_in_est": 999, "tokens_out_est": 999,
                 "tokens_in": 10, "tokens_out": 38, "cost_usd": 0.0188989},
    )
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="cognition",
        payload={"latency_ms": 50, "tokens_in_est": 200, "tokens_out_est": 30},
    )
    totals = store.trace_totals(goal_id="g")
    # real row contributes its real numbers; legacy row falls back to its est
    assert totals["cognition_tokens_in"] == 10 + 200
    assert totals["cognition_tokens_out"] == 38 + 30
    assert totals["cognition_rows_with_real_usage"] == 1
    assert totals["cognition_rows_estimated"] == 1
    assert totals["cognition_cost_usd"] == pytest.approx(0.018899, abs=1e-6)
    # legacy pure-estimate sums kept for back-compat
    assert totals["cognition_tokens_in_est"] == 999 + 200
    assert totals["cognition_tokens_out_est"] == 999 + 30


# ---- trace retention prune (harden/trace-retention, 2026-07-15) --------------
# Production evidence: 402MB devclaw.db, 200k+ trace rows. The prune is
# StateStore-owned maintenance: daily watermark in `meta`, bounded DELETE
# batches so a huge first prune can never wedge a heartbeat tick.

_DAY_MS = 24 * 3600 * 1000
_NOW = 1_800_000_000_000


def _old_row(store, *, age_days: int, tag: str = "old"):
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="note", payload={"tag": tag},
        ts=_NOW - age_days * _DAY_MS,
    )


def test_trace_prune_deletes_old_rows_and_keeps_recent(store):
    _old_row(store, age_days=31, tag="stale")
    _old_row(store, age_days=1, tag="fresh")
    deleted = store.maybe_prune_traces(now_ms=_NOW, retention_days=30)
    assert deleted == 1
    rows = store.read_traces(goal_id="g")
    assert [r["payload"]["tag"] for r in rows] == ["fresh"]


def test_trace_prune_runs_at_most_once_per_day(store):
    _old_row(store, age_days=40)
    assert store.maybe_prune_traces(now_ms=_NOW, retention_days=30) == 1
    # New stale rows appear, but the daily watermark gates the next cycle...
    _old_row(store, age_days=40)
    assert store.maybe_prune_traces(now_ms=_NOW + 3600 * 1000, retention_days=30) == 0
    assert len(store.read_traces(goal_id="g")) == 1
    # ...until a day has passed.
    assert store.maybe_prune_traces(now_ms=_NOW + 25 * 3600 * 1000, retention_days=30) == 1
    assert store.read_traces(goal_id="g") == []


def test_trace_prune_drains_backlog_in_batches_across_ticks(store):
    """A batch that comes back FULL leaves the watermark alone, so the next
    tick continues the drain immediately — a 200k-row first prune spreads
    across ticks instead of blocking one tick for the whole table."""
    for i in range(5):
        _old_row(store, age_days=40, tag=f"stale-{i}")
    assert store.maybe_prune_traces(now_ms=_NOW, retention_days=30, batch_limit=2) == 2
    assert store.maybe_prune_traces(now_ms=_NOW + 1, retention_days=30, batch_limit=2) == 2
    # Short batch → drained → watermark stamps, daily gate takes over.
    assert store.maybe_prune_traces(now_ms=_NOW + 2, retention_days=30, batch_limit=2) == 1
    assert store.read_traces(goal_id="g") == []
    _old_row(store, age_days=40)
    assert store.maybe_prune_traces(now_ms=_NOW + 3600 * 1000, retention_days=30, batch_limit=2) == 0


def test_trace_prune_disabled_by_zero_negative_or_invalid_env(store, monkeypatch):
    """0 / negative / unparseable DEVCLAW_TRACE_RETENTION_DAYS disables the
    prune gracefully — a typo must never crash the heartbeat or delete rows."""
    _old_row(store, age_days=400)
    for raw in ("0", "-5", "not-a-number"):
        monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", raw)
        assert store.maybe_prune_traces(now_ms=_NOW) == 0
    assert len(store.read_traces(goal_id="g")) == 1  # nothing deleted


def test_trace_retention_days_env_parsing(monkeypatch):
    from devclaw.state_store.core import trace_retention_days

    monkeypatch.delenv("DEVCLAW_TRACE_RETENTION_DAYS", raising=False)
    assert trace_retention_days() == 30      # unset → default
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "7")
    assert trace_retention_days() == 7
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "0")
    assert trace_retention_days() == 0       # explicit off
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "-3")
    assert trace_retention_days() == 0       # negative → off
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "thirty")
    assert trace_retention_days() == 0       # unparseable → off, gracefully
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "   ")
    assert trace_retention_days() == 30      # blank → default (unset-equivalent)


def test_inprocess_engine_exposes_prune_seam(store, monkeypatch):
    """The goal heartbeat reaches the prune through the engine (getattr seam,
    same as the quota-pause accessors) — InProcessEngine must delegate to the
    store's maybe_prune_traces."""
    from devclaw.goal.engine import InProcessEngine
    from devclaw.state_store import _now_ms
    from devclaw.task_queue import TaskQueue

    monkeypatch.delenv("DEVCLAW_TRACE_RETENTION_DAYS", raising=False)
    # Age relative to the REAL clock — the engine seam takes no now_ms.
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="note", payload={"tag": "stale"},
        ts=_now_ms() - 40 * _DAY_MS,
    )
    engine = InProcessEngine(TaskQueue(store), store)
    assert engine.prune_traces() == 1
    assert store.read_traces(goal_id="g") == []
