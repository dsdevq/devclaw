"""L8 scorecard telemetry tests — proves the rollup reads what's actually in
state_store, over a window, without any cognition call."""
from __future__ import annotations

import json
import time

import pytest

from devclaw.state_store import StateStore, _now_ms
from devclaw.telemetry import compute_scorecard, format_scorecard


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _land_task(store: StateStore, *, workspace: str, status: str, pr_url: str = "") -> str:
    """Create a task and drive it to a terminal state as if the queue had run
    it. Bypasses TaskQueue — the scorecard reads state_store directly, so we
    exercise the state_store surface without the async runner in the way."""
    tid = f"tid-{time.time_ns()}"
    store.create_task(id=tid, kind="implement_feature", workspace_dir=workspace, goal="g")
    if status == "done":
        store.mark_done(tid, json.dumps({"ok": True}), pr_url=pr_url or None)
    elif status == "failed":
        store.mark_failed(tid, "boom")
    elif status == "cancelled":
        store.mark_task_cancelled(tid)
    return tid


def _emit_evaluator_verdict(store: StateStore, goal_id: str, verdict: str) -> None:
    """Simulate a cognition trace row the evaluator would emit — enough of the
    real shape (role + response_preview) for compute_scorecard to classify it."""
    store.append_trace_event(
        trace_id=f"trace-{time.time_ns()}",
        goal_id=goal_id,
        kind="cognition",
        payload={
            "kind": "cognition",
            "role": "evaluator",
            "model": "sonnet",
            "response_preview": json.dumps({"verdict": verdict, "rationale": "test"})[:240],
        },
    )


def test_empty_store_returns_zero_metrics(store):
    sc = compute_scorecard(store, window_hours=24)
    assert sc["tasks"]["total_terminal"] == 0
    assert sc["merge_rate"] == 0.0
    assert sc["workspace_breaks_tripped"] == 0
    assert sc["evaluator"]["total_calls"] == 0
    assert sc["evaluator"]["steer_rate"] == 0.0
    assert sc["evaluator"]["first_pass_hit_rate"] == 0.0
    assert isinstance(sc["estimate_notes"], list) and len(sc["estimate_notes"]) >= 1


def test_task_counts_and_merge_rate(store):
    _land_task(store, workspace="/w", status="done", pr_url="https://gh/x/1")
    _land_task(store, workspace="/w", status="done", pr_url="https://gh/x/2")
    _land_task(store, workspace="/w", status="done")  # no pr_url — counts as done, not merged
    _land_task(store, workspace="/w", status="failed")
    _land_task(store, workspace="/w", status="cancelled")

    sc = compute_scorecard(store, window_hours=24)
    assert sc["tasks"]["total_terminal"] == 5
    assert sc["tasks"]["done"] == 3
    assert sc["tasks"]["failed"] == 1
    assert sc["tasks"]["cancelled"] == 1
    assert sc["tasks"]["merged_with_pr"] == 2
    # 2 of 3 done tasks carry a pr_url
    assert sc["merge_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_evaluator_verdicts_and_derived_rates(store):
    for _ in range(3):
        _emit_evaluator_verdict(store, "g1", "achieved")
    for _ in range(2):
        _emit_evaluator_verdict(store, "g1", "off_track")
    _emit_evaluator_verdict(store, "g1", "on_track")

    sc = compute_scorecard(store, window_hours=24)
    v = sc["evaluator"]["verdicts"]
    assert v["achieved"] == 3
    assert v["off_track"] == 2
    assert v["on_track"] == 1
    assert v["stalled"] == 0
    assert v["needs_human"] == 0
    assert sc["evaluator"]["total_calls"] == 6
    # 2 off_track out of 6 classified → steer_rate 33.3%
    assert sc["evaluator"]["steer_rate"] == pytest.approx(2 / 6, abs=1e-4)
    # 3 achieved out of 6 classified → first_pass_hit_rate 50%
    assert sc["evaluator"]["first_pass_hit_rate"] == pytest.approx(3 / 6, abs=1e-4)


def test_non_evaluator_cognition_is_ignored(store):
    """Planner / decomposer / grill cognition rows must NOT count toward the
    evaluator rollup — they share the ``cognition`` trace kind but ``role``
    is the discriminator."""
    store.append_trace_event(
        trace_id="t1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "planner",
                 "response_preview": json.dumps({"decision": "act"})[:240]},
    )
    store.append_trace_event(
        trace_id="t2", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "grill",
                 "response_preview": "some prose"},
    )
    _emit_evaluator_verdict(store, "g", "achieved")

    sc = compute_scorecard(store, window_hours=24)
    assert sc["evaluator"]["total_calls"] == 1
    assert sc["evaluator"]["verdicts"]["achieved"] == 1


def test_unparseable_response_lands_in_the_unparseable_bucket(store):
    store.append_trace_event(
        trace_id="t1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "response_preview": "the model just returned prose without JSON"},
    )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["evaluator"]["total_calls"] == 1
    assert sc["evaluator"]["unparseable_responses"] == 1
    # nothing counted in verdicts, so steer/first-pass rates stay 0
    assert sum(sc["evaluator"]["verdicts"].values()) == 0


def test_window_excludes_old_rows(store):
    # A completed task backdated to 8 days ago; a 1-week window should ignore it.
    tid = f"tid-old"
    store.create_task(id=tid, kind="implement_feature", workspace_dir="/w", goal="g")
    store.mark_done(tid, json.dumps({"ok": True}), pr_url="https://gh/x/1")
    old_ms = _now_ms() - int(8 * 24 * 3600 * 1000)
    with store._lock:
        store._db.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ms, tid))
        store._db.commit()

    _land_task(store, workspace="/w", status="done", pr_url="https://gh/x/2")

    sc = compute_scorecard(store, window_hours=168)
    assert sc["tasks"]["total_terminal"] == 1
    assert sc["tasks"]["merged_with_pr"] == 1


def test_workspace_break_events_counted(store):
    # simulate two trip events landing at "now"
    for i in range(2):
        store.append_event(
            task_id=f"tid-{i}", program_id=None,
            type="workspace_break_tripped", source="devclaw",
            payload_json=json.dumps({"workspace_dir": "/w"}),
        )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["workspace_breaks_tripped"] == 2


def test_format_scorecard_smoke(store):
    """format_scorecard must render every metric — a smoke that catches a
    silently-dropped field better than parametrizing over dict keys."""
    _land_task(store, workspace="/w", status="done", pr_url="https://gh/x/1")
    _emit_evaluator_verdict(store, "g", "achieved")
    _emit_evaluator_verdict(store, "g", "off_track")

    text = format_scorecard(compute_scorecard(store, window_hours=24))
    for token in (
        "window:", "tasks (terminal):", "merged with PR:",
        "workspace breaks:", "evaluator calls:", "verdicts:",
        "steer rate:", "first-pass hit:", "estimate notes:",
    ):
        assert token in text, f"format_scorecard dropped {token!r}"


def _emit_evaluator_with_structural(store: StateStore, goal_id: str, verdict: str, grade: str) -> None:
    """Simulate a done-gate evaluator response that carries both verdict AND
    the new C3 structural_health grade. Preview is capped at 240 chars — real
    tracer's cap — so the extractor's regex must hit within that."""
    store.append_trace_event(
        trace_id=f"trace-{time.time_ns()}",
        goal_id=goal_id,
        kind="cognition",
        payload={
            "kind": "cognition",
            "role": "evaluator",
            "model": "sonnet",
            "response_preview": json.dumps(
                {"verdict": verdict, "structural_health": grade, "rationale": "test"}
            )[:240],
        },
    )


def test_structural_grades_counted_per_done_gate_response(store):
    """C3: done-gate responses now carry structural_health. Telemetry counts
    the grade distribution; progress-check calls (no structural_health) don't
    inflate the denominator."""
    _emit_evaluator_with_structural(store, "g", "achieved", "clean")
    _emit_evaluator_with_structural(store, "g", "achieved", "clean")
    _emit_evaluator_with_structural(store, "g", "off_track", "poor")
    _emit_evaluator_with_structural(store, "g", "off_track", "concerns")
    # A progress-check response without structural_health — should NOT count.
    _emit_evaluator_verdict(store, "g", "on_track")

    sc = compute_scorecard(store, window_hours=24)
    grades = sc["evaluator"]["structural_grades"]
    assert grades == {"clean": 2, "concerns": 1, "poor": 1}
    # verdict counting still works over the full 5 responses.
    assert sc["evaluator"]["total_calls"] == 5


def test_verdict_extracted_from_full_response_text_past_preview_horizon(store):
    """T0.5: the verdict sits AFTER 240 chars of rationale — the legacy preview
    truncates it away, but the full ``response_text`` now carried in the
    payload classifies it. This is exactly the row shape the tracer writes
    since T0.5 (both fields present)."""
    full = json.dumps({"rationale": "r" * 400, "verdict": "achieved"})
    assert '"verdict"' not in full[:240]  # premise: preview alone can't see it
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "response_preview": full[:240], "response_text": full},
    )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["evaluator"]["verdicts"]["achieved"] == 1
    assert sc["evaluator"]["unparseable_responses"] == 0


def test_legacy_preview_only_rows_still_classify(store):
    """Rows written before T0.5 carry only response_preview — they must keep
    classifying (fallback path), alongside new full-text rows."""
    # legacy row: preview only
    store.append_trace_event(
        trace_id="t1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "response_preview": json.dumps({"verdict": "on_track"})[:240]},
    )
    # new row: full text (preview truncated mid-JSON)
    full = json.dumps({"rationale": "x" * 300, "verdict": "off_track"})
    store.append_trace_event(
        trace_id="t2", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "response_preview": full[:240], "response_text": full},
    )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["evaluator"]["verdicts"]["on_track"] == 1
    assert sc["evaluator"]["verdicts"]["off_track"] == 1
    assert sc["evaluator"]["unparseable_responses"] == 0


def test_structural_grade_extracted_from_full_response_text(store):
    """The axis-B structural_health grade also benefits from the full text —
    a done-gate response whose grade sits past the preview horizon."""
    full = json.dumps(
        {"rationale": "y" * 300, "verdict": "achieved", "structural_health": "clean"}
    )
    store.append_trace_event(
        trace_id="t", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "response_preview": full[:240], "response_text": full},
    )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["evaluator"]["structural_grades"]["clean"] == 1


def _land_task_with_usage(store: StateStore, *, pr_url: str = "", usage: dict | None = None) -> str:
    """A done task whose result_json carries the runner's per-task usage block
    (mission-control borrow item 2) — the exact payload shape mark_done stores."""
    tid = f"tid-{time.time_ns()}"
    store.create_task(id=tid, kind="implement_feature", workspace_dir="/w", goal="g")
    result: dict = {"status": "ok"}
    if usage is not None:
        result["usage"] = usage
    store.mark_done(tid, json.dumps(result), pr_url=pr_url or None)
    return tid


def test_scorecard_usage_sums_worker_and_cognition_into_tokens_per_merged_pr(store):
    """Item 2's legibility number: the window's total token spend (cognition +
    worker) divided by merged PRs. OAuth runs report no dollar cost, so the
    dollar ratio stays None when no real cost was recorded — tokens are the
    honest cross-billing unit."""
    _land_task_with_usage(
        store, pr_url="https://gh/x/1",
        usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 0, "cost_usd": 0.0},
    )
    _land_task_with_usage(
        store, pr_url="https://gh/x/2",
        usage={"input_tokens": 300, "output_tokens": 200, "cache_read_tokens": 10, "cost_usd": 0.0},
    )
    # Legacy row without a usage block — contributes nothing, breaks nothing.
    _land_task(store, workspace="/w", status="done")
    # Cognition rows: one with REAL usage, one legacy estimate-only.
    store.append_trace_event(
        trace_id="c1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "goal_planner",
                 "tokens_in": 400, "tokens_out": 100, "cost_usd": 0.0},
    )
    store.append_trace_event(
        trace_id="c2", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "goal_planner",
                 "tokens_in_est": 80, "tokens_out_est": 20},
    )

    sc = compute_scorecard(store, window_hours=24)
    u = sc["usage"]
    assert u["worker_input_tokens"] == 1300
    assert u["worker_output_tokens"] == 700
    assert u["worker_cache_read_tokens"] == 10
    assert u["tasks_with_usage"] == 2
    assert u["cognition_tokens_in"] == 480  # 400 real + 80 estimated fallback
    assert u["cognition_tokens_out"] == 120
    assert u["total_tokens"] == 1300 + 700 + 480 + 120
    # 2 merged PRs → integer tokens-per-PR; no dollar cost recorded → None.
    assert u["tokens_per_merged_pr"] == (1300 + 700 + 480 + 120) // 2
    assert u["cost_per_merged_pr_usd"] is None


def test_scorecard_cost_per_merged_pr_when_real_cost_recorded(store):
    _land_task_with_usage(
        store, pr_url="https://gh/x/1",
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cost_usd": 0.30},
    )
    store.append_trace_event(
        trace_id="c1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "evaluator",
                 "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.10,
                 "response_preview": json.dumps({"verdict": "achieved"})[:240]},
    )
    sc = compute_scorecard(store, window_hours=24)
    u = sc["usage"]
    assert u["total_cost_usd"] == pytest.approx(0.40)
    assert u["cost_per_merged_pr_usd"] == pytest.approx(0.40)


def test_scorecard_usage_zero_prs_reports_null_ratios(store):
    _land_task_with_usage(
        store,  # done but NO pr_url → merged_with_pr stays 0
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cost_usd": 0.0},
    )
    sc = compute_scorecard(store, window_hours=24)
    assert sc["usage"]["tokens_per_merged_pr"] is None
    assert sc["usage"]["cost_per_merged_pr_usd"] is None


def test_format_scorecard_renders_usage_and_per_pr_line(store):
    _land_task_with_usage(
        store, pr_url="https://gh/x/1",
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cost_usd": 0.0},
    )
    text = format_scorecard(compute_scorecard(store, window_hours=24))
    assert "usage:" in text
    assert "per merged PR:" in text


def test_format_scorecard_renders_structural_when_any_reported(store):
    """format_scorecard shows structural block only when the window contained
    at least one graded response — an all-zero row would be noise."""
    _emit_evaluator_with_structural(store, "g", "achieved", "clean")
    text = format_scorecard(compute_scorecard(store, window_hours=24))
    assert "structural (done-gate only):" in text
    assert "clean" in text and "concerns" in text and "poor" in text

    # Empty-store case: no structural block.
    empty_store = StateStore(":memory:")
    try:
        empty_text = format_scorecard(compute_scorecard(empty_store, window_hours=24))
        assert "structural (done-gate only):" not in empty_text
    finally:
        empty_store.close()
