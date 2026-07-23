"""Regression test for /problems.json (ADR 0009 P2, PR-A).

The problem-lifecycle tracker renders each catalog entry as *identified → filed
→ resolved*. That only works if the route (a) surfaces the self-issue-filing
Stage-1 fields (issue_number/issue_state) that list_problems omits by default,
and (b) derives the lifecycle stage HONESTLY — a filed & open issue is ``filed``
(in the backlog), never "being auto-fixed" (§5.5: fixing is propose-only).
"""

from __future__ import annotations

import json

import pytest

from devclaw.state_store import StateStore
from devclaw.server.http import _problem_lifecycle


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


def _fp(store, kind):
    return next(p["fingerprint"] for p in store.list_problems() if p["kind"] == kind)


@pytest.fixture
def http_mod(store, monkeypatch):
    from devclaw.server import http as http_mod

    # Three problems: one never filed, one filed+open, one filed+closed.
    store.record_problem(category="task_fail", kind="never", message="a", recovered=False)
    store.record_problem(category="task_fail", kind="open", message="b", recovered=False)
    store.record_problem(category="task_fail", kind="closed", message="c", recovered=False)
    store.set_problem_issue(_fp(store, "open"), issue_number=42, issue_state="open")
    store.set_problem_issue(_fp(store, "closed"), issue_number=7, issue_state="closed")

    monkeypatch.setattr(http_mod, "store", store)
    return http_mod


def test_problem_lifecycle_derivation_is_honest():
    assert _problem_lifecycle({"issue_number": None, "issue_state": None}) == "identified"
    assert _problem_lifecycle({"issue_number": 42, "issue_state": "open"}) == "filed"
    assert _problem_lifecycle({"issue_number": 7, "issue_state": "closed"}) == "resolved"
    # A filed-but-open issue is "filed", NOT "fixing"/"auto-fixing" — §5.5.
    assert _problem_lifecycle({"issue_number": 42, "issue_state": "open"}) != "fixing"


async def test_problems_json_surfaces_issue_fields_and_lifecycle(http_mod):
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/problems.json",
                   "query_string": b"", "headers": []})
    resp = await http_mod.problems_json(req)
    body = json.loads(resp.body)

    assert resp.status_code == 200
    assert body["count"] == 3
    by_kind = {p["kind"]: p for p in body["problems"]}

    assert by_kind["never"]["lifecycle"] == "identified"
    assert by_kind["never"]["issue_number"] is None

    assert by_kind["open"]["lifecycle"] == "filed"
    assert by_kind["open"]["issue_number"] == 42
    assert by_kind["open"]["issue_state"] == "open"

    assert by_kind["closed"]["lifecycle"] == "resolved"
    assert by_kind["closed"]["issue_state"] == "closed"


def test_list_problems_default_omits_issue_fields(store):
    # The default (MCP/test) output stays byte-identical — issue fields only
    # appear when the console route asks for them.
    store.record_problem(category="task_fail", kind="x", message="m", recovered=False)
    row = store.list_problems()[0]
    assert "issue_number" not in row
    assert "issue_number" in store.list_problems(include_issue=True)[0]
