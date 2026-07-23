"""Regression test for the NODE view vitals endpoint (ADR 0008 P1, PR-B).

``/node.json`` is the top of the console drill-down spine. It assembles vitals
from projections that already exist — dispatch/heartbeat state, goal population,
the clean-cycle headline, and the 5-layer strip — read-only, no new cognition.
These pin the two things easy to regress:

  * goal population is bucketed the SAME way the morning digest triages
    (cancelled/done terminal; needs-you = blocked OR stalled OR stop-state;
    else running) — a mis-bucket would misreport what needs the operator.
  * the 5-layer strip stays HONEST: L1 up (serving), L2 the dispatch state, L4
    from live tasks; L3/L5 ``unknown`` (no idle probe yet) — never a faked
    "healthy" signal the ADR forbids.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from devclaw.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


def _fake_goals(rows):
    return SimpleNamespace(list_goals=lambda: rows)


def _goal(phase="idle", *, blocked_on=None, stalled=False, direction=None):
    return {
        "phase": phase,
        "blocked_on": blocked_on,
        "progress": {"stalled": stalled},
        "direction": direction,
    }


@pytest.fixture
def http_mod(store, monkeypatch):
    from devclaw.server import http as http_mod

    monkeypatch.setattr(http_mod, "store", store)
    return http_mod


def test_node_vitals_buckets_goal_population_like_the_digest(http_mod, monkeypatch):
    monkeypatch.setattr(
        http_mod,
        "goals",
        _fake_goals([
            _goal("idle"),                                  # running
            _goal("in_flight"),                             # running
            _goal("blocked", blocked_on="merge PR #6"),     # needs-you (blocked)
            _goal("idle", stalled=True),                    # needs-you (stalled)
            _goal("idle", direction="needs_human"),         # needs-you (stop-state)
            _goal("done"),                                  # done
            _goal("achieved"),                              # done
            _goal("cancelled"),                             # cancelled
        ]),
    )
    v = http_mod._node_vitals()

    assert v["goals"] == {
        "total": 8,
        "running": 2,
        "needsYou": 3,
        "done": 2,
        "cancelled": 1,
    }


def test_node_vitals_clean_cycle_headline_is_the_newest_window(http_mod, monkeypatch):
    monkeypatch.setattr(http_mod, "goals", _fake_goals([]))
    http_mod.store.record_cycle_report(
        cycle_key="c1", window_start_ms=0, window_end_ms=1000,
        clean=True, wedges_json="[]", pauses_json="[]", summary="ok",
    )
    http_mod.store.record_cycle_report(
        cycle_key="c2", window_start_ms=1000, window_end_ms=2000,
        clean=False, wedges_json='[{"class":"x"}]', pauses_json="[]", summary="wedge",
    )
    v = http_mod._node_vitals()

    # Newest window (c2) is the headline; recent rate counts the clean ones.
    assert v["cleanCycle"]["clean"] is False
    assert v["cleanCycle"]["lastWindowEndMs"] == 2000
    assert v["cleanCycle"]["recent"] == {"clean": 1, "total": 2}


def test_node_vitals_clean_cycle_null_when_no_reports(http_mod, monkeypatch):
    monkeypatch.setattr(http_mod, "goals", _fake_goals([]))
    v = http_mod._node_vitals()
    assert v["cleanCycle"]["clean"] is None
    assert v["cleanCycle"]["recent"] == {"clean": 0, "total": 0}


def test_node_vitals_layers_stay_honest(http_mod, monkeypatch):
    monkeypatch.setattr(http_mod, "goals", _fake_goals([]))
    http_mod.store.create_task(id="t1", kind="implement_feature", workspace_dir="/w", goal="g")
    http_mod.store.mark_running("t1")
    v = http_mod._node_vitals()

    layers = {l["key"]: l["status"] for l in v["layers"]}
    assert layers["mcp"] == "up"          # serving the request proves L1
    assert layers["goal"] == "up"         # no hold/pause on a fresh store
    assert layers["cognition"] == "unknown"  # honest — no idle probe
    assert layers["engine"] == "active"   # one running task
    assert layers["worker"] == "unknown"  # honest — no idle probe
    assert v["runningTasks"] == 1


async def test_node_json_route_returns_vitals(http_mod, monkeypatch):
    monkeypatch.setattr(http_mod, "goals", _fake_goals([]))
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/node.json",
                   "query_string": b"", "headers": []})
    resp = await http_mod.node_json(req)
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert set(body) >= {"version", "dispatch", "goals", "cleanCycle", "layers"}
