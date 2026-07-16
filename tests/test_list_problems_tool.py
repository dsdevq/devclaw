"""The ``list_problems`` MCP tool — the read surface over the deduplicated
problems catalog (#260). The catalog's capture/dedup/count layer already had a
store method (``StateStore.list_problems``) and pins in
``test_problems_catalog.py``; what was missing was an MCP tool exposing it, so
the devclaw-status digest could stop reporting "captured but not readable over
MCP". These pins guard the tool wrapper: it returns the store's rows most-
frequent first, honors the category filter, respects the limit, and stays a
pure read (no cognition call).
"""

from __future__ import annotations

import json

import pytest

from devclaw.server import tools as _tools
from devclaw.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "problems.db"))


@pytest.fixture(autouse=True)
def _patch_store(store, monkeypatch):
    # tools.py binds `store` at import; point it at a throwaway catalog.
    monkeypatch.setattr(_tools, "store", store)
    return store


async def test_list_problems_ranks_most_frequent_first(store):
    # A rare terminal failure and a frequent one; the frequent must rank first.
    store.record_problem(category="gate", kind="crash", message="rare boom", recovered=False)
    for _ in range(3):
        store.record_problem(
            category="task_fail", kind="timeout", message="claude timed out", recovered=False
        )

    out = json.loads(await _tools.list_problems())
    assert out["count"] == 2  # two DISTINCT problems, not four occurrences
    assert [p["count"] for p in out["problems"]] == [3, 1]
    assert out["problems"][0]["category"] == "task_fail"


async def test_list_problems_filters_by_category(store):
    store.record_problem(category="gate", kind="crash", message="gate boom", recovered=False)
    store.record_problem(category="limit", kind="usage", message="hit the cap", recovered=True)

    out = json.loads(await _tools.list_problems(category="limit"))
    assert out["count"] == 1
    assert out["problems"][0]["category"] == "limit"
    assert out["problems"][0]["recovered_count"] == 1


async def test_list_problems_respects_limit(store):
    for i in range(5):
        store.record_problem(
            category="cognition", kind=f"k{i}", message=f"distinct problem {i}", recovered=False
        )

    out = json.loads(await _tools.list_problems(limit=2))
    assert out["count"] == 2  # capped, though five distinct rows exist


async def test_list_problems_empty_catalog_returns_empty(store):
    out = json.loads(await _tools.list_problems())
    assert out == {"count": 0, "problems": []}
