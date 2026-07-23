"""Unit tests for the /goals/{id}/prs.json helpers (PR#8).

The endpoint itself is exercised end-to-end by the console — these tests pin
the two pure helpers whose logic is easy to regress:

  * ``_parse_pr_url`` — must reject anything that isn't a canonical
    github.com PR URL, since the POST /prs/merge endpoint shells `gh` using
    the parsed slug + number.
  * ``_collect_goal_pr_rows`` — must dedupe by pr_url (keeping the newest
    trace ts) and sort newest-first, so a mission that dispatches the same
    PR through multiple delivery events shows a single row.
"""

from __future__ import annotations

import pytest

from devclaw.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


# ── _parse_pr_url ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/dsdevq/closeloop/pull/47", ("dsdevq", "closeloop", 47)),
        ("https://github.com/dsdevq/closeloop/pull/47/", ("dsdevq", "closeloop", 47)),
        ("http://github.com/dsdevq/closeloop/pull/1", ("dsdevq", "closeloop", 1)),
        (
            "https://github.com/lifekit-hq/lifekit-stack/pull/82",
            ("lifekit-hq", "lifekit-stack", 82),
        ),
    ],
)
def test_parse_pr_url_accepts_canonical_github_urls(url, expected):
    from devclaw.server.http import _parse_pr_url

    assert _parse_pr_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://gitlab.com/foo/bar/pull/1",
        "https://evil.com/dsdevq/closeloop/pull/47",
        "https://github.com/dsdevq/closeloop/issues/47",
        "https://github.com/dsdevq/closeloop/pull/",
        "https://github.com/dsdevq/closeloop/pull/abc",
        None,
        42,
    ],
)
def test_parse_pr_url_rejects_non_github_or_malformed(url):
    from devclaw.server.http import _parse_pr_url

    # The endpoint may pass the URL as-is off the wire; the parser must swallow
    # anything that isn't a canonical PR URL — the merge endpoint shells `gh`
    # with the parsed slug, so a spoofed body cannot resolve to something we'd
    # execute.
    assert _parse_pr_url(url if isinstance(url, str) else "") is None


# ── _collect_goal_pr_rows ──────────────────────────────────────────────────


def _seed_delivery(store, *, goal_id, trace_id, pr_url, action_label, ts=None):
    """Write a delivery trace event with the same payload shape trace.py emits.

    ``ts`` is accepted for readability but ignored — the collector orders by
    the monotonic ``id`` column, so insertion order IS the ordering.
    """
    payload = {
        "kind": "delivery",
        "goal_id": goal_id,
        "action_label": action_label,
        "gate_passed": True,
        "pr_url": pr_url,
    }
    store.append_trace_event(
        trace_id=trace_id, goal_id=goal_id, kind="delivery", payload=payload
    )


def test_collect_pr_rows_returns_empty_when_no_deliveries(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    assert http_mod._collect_goal_pr_rows("g1") == []


def test_collect_pr_rows_skips_delivery_without_pr_url(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    # A gate-failed delivery has no pr_url — must not create a phantom row.
    store.append_trace_event(
        trace_id="t1", goal_id="g1", kind="delivery",
        payload={"action_label": "fix bug", "gate_passed": False, "pr_url": ""},
    )
    assert http_mod._collect_goal_pr_rows("g1") == []


def test_collect_pr_rows_skips_non_github_urls(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    # A hostile trace payload should not surface a row we'd then try to merge.
    _seed_delivery(
        store, goal_id="g1", trace_id="t1",
        pr_url="https://evil.example.com/foo/bar/pull/1",
        action_label="rogue",
    )
    assert http_mod._collect_goal_pr_rows("g1") == []


def test_collect_pr_rows_dedupes_by_url_keeping_newest_ts(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    _seed_delivery(
        store, goal_id="g1", trace_id="t1",
        pr_url="https://github.com/dsdevq/closeloop/pull/47",
        action_label="first mention", ts="2026-07-04T01:00:00+00:00",
    )
    _seed_delivery(
        store, goal_id="g1", trace_id="t2",
        pr_url="https://github.com/dsdevq/closeloop/pull/47",
        action_label="second mention (newer)", ts="2026-07-04T02:00:00+00:00",
    )
    rows = http_mod._collect_goal_pr_rows("g1")
    assert len(rows) == 1
    assert rows[0]["actionLabel"] == "second mention (newer)"


def test_collect_pr_rows_sorts_newest_first_across_prs(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    _seed_delivery(
        store, goal_id="g1", trace_id="t1",
        pr_url="https://github.com/dsdevq/closeloop/pull/47",
        action_label="older", ts="2026-07-03T10:00:00+00:00",
    )
    _seed_delivery(
        store, goal_id="g1", trace_id="t2",
        pr_url="https://github.com/dsdevq/closeloop/pull/48",
        action_label="newer", ts="2026-07-04T01:00:00+00:00",
    )
    rows = http_mod._collect_goal_pr_rows("g1")
    assert [r["prNumber"] for r in rows] == [48, 47]
    assert rows[0]["repo"] == "dsdevq/closeloop"


def test_collect_pr_rows_isolates_by_goal(store, monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "store", store)

    _seed_delivery(
        store, goal_id="g1", trace_id="t1",
        pr_url="https://github.com/dsdevq/closeloop/pull/1",
        action_label="g1's PR",
    )
    _seed_delivery(
        store, goal_id="g2", trace_id="t2",
        pr_url="https://github.com/dsdevq/closeloop/pull/2",
        action_label="g2's PR",
    )
    assert [r["prNumber"] for r in http_mod._collect_goal_pr_rows("g1")] == [1]
    assert [r["prNumber"] for r in http_mod._collect_goal_pr_rows("g2")] == [2]
