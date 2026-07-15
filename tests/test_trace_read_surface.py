"""The first-class telemetry READ surface (trace list / report / /traces.json).

Before this, answering "what happened overnight" meant hand-writing sqlite
against a docker-cp'd DB snapshot. These tests pin the three new read paths —
all pure SELECTs (single-writer invariant untouched):

  * ``StateStore.read_traces`` filter extensions — role / since_ms /
    errors_only / newest_first, filtered in SQL (the production table holds
    200k+ rows; a load-all-then-filter regression is exactly what this file
    exists to catch).
  * ``devclaw trace list`` / ``devclaw trace report`` (CLI) — human one-line
    output + --json, and the deterministic no-LLM day-report aggregates.
  * ``GET /traces.json`` — same filters over HTTP.

Fixture style follows tests/test_telemetry_traces.py (a real StateStore on a
tmp path, payload shapes copied from loom/trace.py's dataclasses).
"""

from __future__ import annotations

import json
import time

import pytest
from starlette.requests import Request

from devclaw.cli import main
from devclaw.state_store import StateStore
from devclaw.telemetry import compute_trace_report, format_trace_report, parse_since

NOW_MS = int(time.time() * 1000)
HOUR_MS = 3_600_000


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "traces.db"))
    yield s
    s.close()


def _seed_representative_rows(store: StateStore) -> None:
    """A representative overnight window: cognition (incl. one TIMEOUT), a
    dispatch, notifications at both altitudes, trend checks, plus tasks with
    a retry storm and a failed settle. Payload shapes mirror trace.py."""
    # -- traces -----------------------------------------------------------
    store.append_trace_event(
        trace_id="t1", goal_id="g1", kind="cognition", ts=NOW_MS - 3 * HOUR_MS,
        payload={"kind": "cognition", "role": "planner", "model": "claude-sonnet",
                 "latency_ms": 100, "error": ""},
    )
    store.append_trace_event(
        trace_id="t1", goal_id="g1", kind="cognition", ts=NOW_MS - 2 * HOUR_MS,
        payload={"kind": "cognition", "role": "planner", "model": "claude-sonnet",
                 "latency_ms": 300, "error": ""},
    )
    # the timeout cognition event — planner call that hit PLANNER_TIMEOUT_MS
    store.append_trace_event(
        trace_id="t2", goal_id="g1", kind="cognition", ts=NOW_MS - 2 * HOUR_MS,
        payload={"kind": "cognition", "role": "planner", "model": "claude-sonnet",
                 "latency_ms": 60000, "error": "timeout"},
    )
    store.append_trace_event(
        trace_id="t2", goal_id="g2", kind="cognition", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "cognition", "role": "evaluator", "model": "claude-opus",
                 "latency_ms": 500, "error": ""},
    )
    store.append_trace_event(
        trace_id="t2", goal_id="g2", kind="dispatch", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "dispatch", "tool": "implement_feature", "ref_id": "task-1",
                 "engine": "stub"},
    )
    store.append_trace_event(
        trace_id="t2", goal_id="g2", kind="notify", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "notify", "level": "OWNER", "text": "goal blocked: needs answer"},
    )
    store.append_trace_event(
        trace_id="t2", goal_id="g2", kind="notify", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "notify", "level": "TASK", "text": "dispatched backlog item"},
    )
    store.append_trace_event(
        trace_id="t3", goal_id="g1", kind="trend_check", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "trend_check", "signal": "R2", "scope": "per_project",
                 "fired": True, "reason": "fired"},
    )
    store.append_trace_event(
        trace_id="t3", goal_id="g1", kind="trend_check", ts=NOW_MS - 1 * HOUR_MS,
        payload={"kind": "trend_check", "signal": "R2", "scope": "harness_self",
                 "fired": False, "reason": "below_threshold"},
    )
    # an OLD cognition row, outside any since window the tests use
    store.append_trace_event(
        trace_id="t0", goal_id="g1", kind="cognition", ts=NOW_MS - 100 * HOUR_MS,
        payload={"kind": "cognition", "role": "planner", "model": "claude-sonnet",
                 "latency_ms": 999, "error": "spawn failed: ancient"},
    )
    # -- tasks: a retry storm (same title x2, one failed) + a clean done ---
    store.create_task(id="task-a1", kind="implement_feature", workspace_dir="/ws",
                      goal="add /health", title="add /health endpoint")
    store.mark_running("task-a1")
    store.mark_failed("task-a1", "review gate crashed: non-JSON verdict")
    store.create_task(id="task-a2", kind="implement_feature", workspace_dir="/ws",
                      goal="add /health", title="add /health endpoint")
    store.mark_running("task-a2")
    store.mark_done("task-a2", "{}")
    store.create_task(id="task-b", kind="fix_bug", workspace_dir="/ws",
                      goal="fix login", title="fix login redirect")
    store.mark_running("task-b")
    store.mark_failed("task-b", "claude --print timed out after 240000ms")


# ---- StateStore.read_traces filter extensions ------------------------------


def test_read_traces_filters_role_in_sql_across_goals(store):
    _seed_representative_rows(store)
    rows = store.read_traces(role="evaluator")
    assert [r["payload"]["role"] for r in rows] == ["evaluator"]
    assert rows[0]["goal_id"] == "g2"
    # role composes with the pre-existing goal_id filter
    assert store.read_traces(goal_id="g1", role="evaluator") == []


def test_read_traces_since_ms_excludes_older_rows(store):
    _seed_representative_rows(store)
    rows = store.read_traces(kind="cognition", since_ms=NOW_MS - 24 * HOUR_MS)
    assert len(rows) == 4  # the 100h-old planner row is excluded
    assert all(r["ts"] >= NOW_MS - 24 * HOUR_MS for r in rows)


def test_read_traces_errors_only_returns_only_error_rows(store):
    _seed_representative_rows(store)
    rows = store.read_traces(errors_only=True)
    assert len(rows) == 2  # the timeout + the ancient spawn failure
    assert all(r["payload"]["error"] for r in rows)
    # composes with since_ms: only the in-window timeout survives
    recent = store.read_traces(errors_only=True, since_ms=NOW_MS - 24 * HOUR_MS)
    assert [r["payload"]["error"] for r in recent] == ["timeout"]


def test_read_traces_newest_first_orders_by_id_desc(store):
    _seed_representative_rows(store)
    rows = store.read_traces(newest_first=True, limit=3)
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)
    # limit + DESC == "the last N events", not "the first N ever"
    all_rows = store.read_traces(limit=10_000)
    assert ids[0] == all_rows[-1]["id"]


def test_read_traces_existing_goal_kind_call_shape_unchanged(store):
    """The pre-existing call sites (get_trace tool, /prs.json dedup) pass
    goal_id+kind and rely on ascending id order — must stay byte-identical."""
    _seed_representative_rows(store)
    rows = store.read_traces(goal_id="g1", kind="cognition")
    assert [r["kind"] for r in rows] == ["cognition"] * 4
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)


# ---- compute_trace_report (deterministic, no LLM) ---------------------------


def test_trace_report_aggregates_cognition_latency_and_timeouts(store):
    _seed_representative_rows(store)
    rep = compute_trace_report(store, since_ms=NOW_MS - 24 * HOUR_MS)
    cog = rep["cognition"]
    assert cog["total_calls"] == 4
    planner = cog["by_role"]["planner"]
    assert planner["calls"] == 3
    assert planner["timeouts"] == 1
    # latencies [100, 300, 60000]: nearest-rank p50=300, p90=max=60000
    assert planner["latency_ms"] == {"p50": 300, "p90": 60000, "max": 60000}
    evaluator = cog["by_role"]["evaluator"]
    assert evaluator == {
        "calls": 1, "errors": 0, "timeouts": 0,
        "latency_ms": {"p50": 500, "p90": 500, "max": 500},
    }


def test_trace_report_counts_tasks_by_status_and_error_class(store):
    _seed_representative_rows(store)
    rep = compute_trace_report(store, since_ms=NOW_MS - 24 * HOUR_MS)
    assert rep["tasks"]["dispatched"] == 3
    assert rep["tasks"]["settled_by_status"] == {"done": 1, "failed": 2}
    # "timed out" classifies as timeout; the gate crash keeps its prefix
    assert rep["tasks"]["failed_error_classes"] == {
        "review gate crashed": 1,
        "timeout": 1,
    }


def test_trace_report_flags_retry_storms_by_repeated_title(store):
    _seed_representative_rows(store)
    rep = compute_trace_report(store, since_ms=NOW_MS - 24 * HOUR_MS)
    assert rep["retry_storms"] == [{"title": "add /health endpoint", "attempts": 2}]


def test_trace_report_counts_owner_notifications_and_trend_checks(store):
    _seed_representative_rows(store)
    rep = compute_trace_report(store, since_ms=NOW_MS - 24 * HOUR_MS)
    assert rep["notifications"]["owner"] == 1
    assert rep["notifications"]["by_level"] == {"OWNER": 1, "TASK": 1}
    assert rep["trend_checks"] == {"total": 2, "fired": 1}


def test_trace_report_renders_human_readable(store):
    _seed_representative_rows(store)
    rep = compute_trace_report(store, since_ms=NOW_MS - 24 * HOUR_MS)
    text = format_trace_report(rep)
    assert "dispatched 3" in text
    assert "timeouts 1" in text
    assert "2x  add /health endpoint" in text
    assert "OWNER 1" in text
    assert "trend checks: 2 (1 fired)" in text


# ---- parse_since ------------------------------------------------------------


def test_parse_since_relative_and_iso_and_rejects_garbage():
    assert parse_since("24h", now_ms=1_000_000_000_000) == 1_000_000_000_000 - 24 * HOUR_MS
    assert parse_since("30m", now_ms=1_000_000_000_000) == 1_000_000_000_000 - 30 * 60_000
    assert parse_since("2d", now_ms=1_000_000_000_000) == 1_000_000_000_000 - 48 * HOUR_MS
    # naive ISO is treated as UTC, matching the tracer's epoch-ms ts
    assert parse_since("1970-01-01T01:00:00") == HOUR_MS
    with pytest.raises(ValueError):
        parse_since("yesterday")
    with pytest.raises(ValueError):
        parse_since("")


# ---- CLI: devclaw trace list / report ---------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    db = tmp_path / "devclaw.db"
    goals = tmp_path / "goals"
    goals.mkdir()
    monkeypatch.setenv("DEVCLAW_DB", str(db))
    monkeypatch.setenv("DEVCLAW_GOALS_DIR", str(goals))
    s = StateStore(str(db))
    _seed_representative_rows(s)
    s.close()
    return db


def test_cli_trace_list_prints_one_line_per_event(cli_env, capsys):
    assert main(["trace", "list", "--kind", "cognition", "--since", "24h"]) == 0
    out = capsys.readouterr().out.rstrip("\n").splitlines()
    assert len(out) == 4
    assert any("ERROR: timeout" in line for line in out)
    assert all("cognition" in line for line in out)


def test_cli_trace_list_filters_role_errors_only_and_emits_json(cli_env, capsys):
    assert main(["trace", "list", "--role", "planner", "--errors-only",
                 "--since", "24h", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["payload"]["error"] == "timeout"
    assert data[0]["payload"]["role"] == "planner"


def test_cli_trace_list_rejects_bad_since(cli_env, capsys):
    assert main(["trace", "list", "--since", "overnight"]) == 1
    assert "bad --since" in capsys.readouterr().err


def test_cli_trace_report_json_matches_compute(cli_env, capsys):
    assert main(["trace", "report", "--since", "24h", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["tasks"]["dispatched"] == 3
    assert rep["cognition"]["by_role"]["planner"]["timeouts"] == 1
    assert rep["retry_storms"][0]["attempts"] == 2


def test_cli_trace_report_human_output(cli_env, capsys):
    assert main(["trace", "report", "--since", "24h"]) == 0
    out = capsys.readouterr().out
    assert "cognition:   4 calls" in out
    assert "retry storms" in out


# ---- HTTP: GET /traces.json --------------------------------------------------


def _get(path_qs: str) -> Request:
    """Build a starlette Request for a GET with the given query string —
    enough for a route that only reads request.query_params."""
    path, _, qs = path_qs.partition("?")
    return Request({
        "type": "http", "method": "GET", "path": path,
        "query_string": qs.encode(), "headers": [],
    })


@pytest.fixture
def http_store(store, monkeypatch):
    from devclaw.server import http as http_mod

    _seed_representative_rows(store)
    monkeypatch.setattr(http_mod, "store", store)
    return http_mod


async def test_traces_json_endpoint_filters_and_orders_newest_first(http_store):
    resp = await http_store.traces_json(
        _get("/traces.json?kind=cognition&role=planner&since=24h")
    )
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["count"] == 3
    assert all(r["payload"]["role"] == "planner" for r in body["traces"])
    ids = [r["id"] for r in body["traces"]]
    assert ids == sorted(ids, reverse=True)


async def test_traces_json_endpoint_errors_only_and_goal_filter(http_store):
    resp = await http_store.traces_json(
        _get("/traces.json?errors_only=1&goal=g1&since=24h")
    )
    body = json.loads(resp.body)
    assert body["count"] == 1
    assert body["traces"][0]["payload"]["error"] == "timeout"


async def test_traces_json_endpoint_caps_limit_and_rejects_bad_params(http_store):
    resp = await http_store.traces_json(_get("/traces.json?limit=999999"))
    assert json.loads(resp.body)["limit"] == 1000  # capped, not honored
    resp = await http_store.traces_json(_get("/traces.json?limit=zero"))
    assert resp.status_code == 400
    resp = await http_store.traces_json(_get("/traces.json?limit=-5"))
    assert resp.status_code == 400
    resp = await http_store.traces_json(_get("/traces.json?since=overnight"))
    assert resp.status_code == 400
    assert json.loads(resp.body)["error"] == "bad_since"
