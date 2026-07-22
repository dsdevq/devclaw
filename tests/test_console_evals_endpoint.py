"""Regression tests for the console Evals surfaces (ADR 0006 PR3):

  * ``GET /evals/outcomes.json`` — read-only projection over ``eval_outcomes``
    (params: limit, source). Pins that it returns the store's rows newest-first,
    honours the source filter, and 400s (never silently mis-filters) on bad
    input.
  * ``GET /evals/cycles.json`` — read-only ``cycle_reports`` list. Pins that an
    empty table returns ``[]`` (never a 500) and that recorded cycles come back;
    the store read re-raises a real ``OperationalError`` (locked/corrupt DB)
    rather than masking it as an empty clean-cycle list.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from starlette.requests import Request

from devclaw.state_store import StateStore


def _store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


def _get(fn, query: str = ""):
    scope = {
        "type": "http",
        "method": "GET",
        "path_params": {},
        "headers": [],
        "query_string": query.encode(),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    resp = asyncio.run(fn(Request(scope, receive)))
    return resp.status_code, json.loads(resp.body)


# ── /evals/outcomes.json ────────────────────────────────────────────────────

def test_evals_outcomes_endpoint_returns_projection_rows(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    store = _store(tmp_path)
    store.record_basket_outcome(
        report_ref="passrate-1.json", ticket="T-1", status="done",
        kind="fix_bug", verify_passed=True, pr_url="https://x/pr/1",
    )
    store.record_basket_outcome(
        report_ref="passrate-1.json", ticket="T-2", status="failed",
        kind="implement_feature", error="review rejected the change",
    )
    monkeypatch.setattr(http_mod, "store", store)
    status, body = _get(http_mod.evals_outcomes_json)
    assert status == 200
    assert isinstance(body, list) and len(body) == 2
    tickets = {r["ticket"] for r in body}
    assert tickets == {"T-1", "T-2"}
    done = next(r for r in body if r["ticket"] == "T-1")
    assert done["status"] == "done" and done["verify_passed"] == 1
    assert done["source"] == "basket"


def test_evals_outcomes_endpoint_honours_source_filter(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    store = _store(tmp_path)
    store.record_basket_outcome(report_ref="r.json", ticket="T-1", status="done")
    monkeypatch.setattr(http_mod, "store", store)
    # basket rows exist; source=basket returns them, source=live returns none.
    _, basket = _get(http_mod.evals_outcomes_json, "source=basket")
    _, live = _get(http_mod.evals_outcomes_json, "source=live")
    assert len(basket) == 1 and basket[0]["ticket"] == "T-1"
    assert live == []


def test_evals_outcomes_endpoint_rejects_bad_source(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", _store(tmp_path))
    status, body = _get(http_mod.evals_outcomes_json, "source=bogus")
    assert status == 400 and body["error"] == "bad_source"


def test_evals_outcomes_endpoint_rejects_bad_limit(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", _store(tmp_path))
    status, body = _get(http_mod.evals_outcomes_json, "limit=nope")
    assert status == 400 and body["error"] == "bad_limit"


# ── /evals/cycles.json ──────────────────────────────────────────────────────

def test_evals_cycles_endpoint_empty_when_no_reports(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    # cycle_reports is bootstrapped by StateStore (PR2) but empty until a
    # window closes; the endpoint returns [] rather than 500ing.
    store = _store(tmp_path)
    monkeypatch.setattr(http_mod, "store", store)
    status, body = _get(http_mod.evals_cycles_json)
    assert status == 200 and body == []


def test_evals_cycles_endpoint_returns_rows_when_table_present(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    store = _store(tmp_path)
    # Record a cycle via PR2's single-writer API (not a hand-rolled CREATE —
    # StateStore already owns the DDL).
    store.record_cycle_report(
        cycle_key="2026-07-21", window_start_ms=1, window_end_ms=2,
        clean=True, wedges_json="[]", pauses_json="[]", summary="clean cycle",
        sent_at=3,
    )
    monkeypatch.setattr(http_mod, "store", store)
    status, body = _get(http_mod.evals_cycles_json)
    assert status == 200 and len(body) == 1
    assert body[0]["cycle_key"] == "2026-07-21" and body[0]["clean"] == 1


def test_list_cycle_reports_reraises_real_operational_error_not_missing_table(tmp_path):
    """The defensive catch degrades to [] ONLY for a genuinely-absent table —
    a real fault (locked/corrupt DB, an OperationalError that is NOT
    ``no such table``) must surface loudly, never read as an empty clean-cycle
    list (loud-failure-over-silent-degradation)."""
    store = _store(tmp_path)

    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    store._db = _Boom()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store.list_cycle_reports(limit=10)
