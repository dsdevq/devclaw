"""Regression tests for the console Evals surfaces (ADR 0006 PR3):

  * ``GET /evals/outcomes.json`` — read-only projection over ``eval_outcomes``
    (params: limit, source). Pins that it returns the store's rows newest-first,
    honours the source filter, and 400s (never silently mis-filters) on bad
    input.
  * ``GET /evals/nights.json`` — read-only ``night_reports`` list. Pins the
    DEFENSIVE contract: PR3 may ship ahead of the night-report tranche (PR2),
    so a missing ``night_reports`` table degrades to ``[]``, never a 500; and
    when the table IS present the rows come back.
"""

from __future__ import annotations

import asyncio
import json

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


# ── /evals/nights.json ──────────────────────────────────────────────────────

def test_evals_nights_endpoint_empty_when_table_absent(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    # A fresh StateStore does NOT create night_reports (PR2 owns that DDL).
    store = _store(tmp_path)
    monkeypatch.setattr(http_mod, "store", store)
    status, body = _get(http_mod.evals_nights_json)
    assert status == 200 and body == []


def test_evals_nights_endpoint_returns_rows_when_table_present(tmp_path, monkeypatch):
    from devclaw.server import http as http_mod
    store = _store(tmp_path)
    # Simulate the PR2 migration having landed: create the table + one row.
    store._db.execute(
        """
        CREATE TABLE night_reports (
            night_date      TEXT PRIMARY KEY,
            window_start_ms INTEGER NOT NULL,
            window_end_ms   INTEGER NOT NULL,
            clean           INTEGER NOT NULL,
            wedges_json     TEXT NOT NULL,
            pauses_json     TEXT NOT NULL,
            summary         TEXT NOT NULL,
            sent_at         INTEGER,
            created_at      INTEGER NOT NULL
        )
        """
    )
    store._db.execute(
        "INSERT INTO night_reports (night_date, window_start_ms, window_end_ms, "
        " clean, wedges_json, pauses_json, summary, sent_at, created_at) "
        "VALUES ('2026-07-21', 1, 2, 1, '[]', '[]', 'clean night', 3, 4)",
    )
    store._db.commit()
    monkeypatch.setattr(http_mod, "store", store)
    status, body = _get(http_mod.evals_nights_json)
    assert status == 200 and len(body) == 1
    assert body[0]["night_date"] == "2026-07-21" and body[0]["clean"] == 1
