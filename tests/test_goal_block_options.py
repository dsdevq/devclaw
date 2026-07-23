"""Regression tests for §6 structured decision blocks (ADR 0010, P3.1a).

Covers the vertical: the planner PARSES structured options at block time
(blank-safe — an absent/malformed field degrades to the pre-§6 free-text
block, never raises), and the store round-trips them so the console can render
click-to-steer buttons.
"""

from __future__ import annotations

from devclaw.goal.models import BlockOption
from devclaw.goal.planner import validate
from devclaw.goal.store import GoalStore
from tests.goal_fakes import seed_goal


# ── planner parse (pure, no DB) ──────────────────────────────────────────────


def test_planner_parses_block_options_and_recommendation():
    r = validate({
        "decision": "blocked",
        "question": "ng-zorro or keep bespoke?",
        "options": [
            {"key": "a", "label": "Migrate", "detail": "big tranche", "steer": "Do the migration."},
            {"key": "b", "label": "Drop it", "steer": "Drop the ng-zorro requirement from done_when."},
        ],
        "recommended": "b",
    })
    assert r.decision == "blocked"
    assert [o.key for o in r.options] == ["a", "b"]
    assert r.options[0].steer == "Do the migration."
    assert r.options[1].detail == ""  # optional, absent
    assert r.recommended == "b"


def test_block_options_absent_is_blank_safe():
    # The pre-§6 shape (question only) must still parse to a plain block.
    r = validate({"decision": "blocked", "question": "genuinely open-ended?"})
    assert r.options == []
    assert r.recommended == ""


def test_block_options_malformed_entries_are_dropped_never_raise():
    r = validate({
        "decision": "blocked",
        "question": "q",
        "options": [
            {"key": "a", "label": "ok", "steer": "s"},        # valid
            {"key": "b", "label": "no steer"},                # missing steer → drop
            {"label": "no key", "steer": "s"},                # missing key → drop
            {"key": "a", "label": "dup", "steer": "s2"},      # duplicate key → drop
            "not-a-dict",                                     # → drop
        ],
        "recommended": "does-not-exist",                     # unknown key → cleared
    })
    assert [o.key for o in r.options] == ["a"]
    assert r.recommended == ""


def test_block_options_non_list_is_ignored():
    r = validate({"decision": "blocked", "question": "q", "options": "nope"})
    assert r.options == []


# ── store round-trip ─────────────────────────────────────────────────────────


def test_block_options_store_round_trip(tmp_path):
    seed_goal(tmp_path, "g1")
    store = GoalStore(tmp_path)
    store.write_block_options(
        "g1",
        [
            BlockOption(key="a", label="A", detail="d", steer="steer A"),
            BlockOption(key="b", label="B", steer="steer B"),
        ],
        "a",
    )
    got = store.read_block_options("g1")
    assert got["recommended"] == "a"
    assert [o["key"] for o in got["options"]] == ["a", "b"]
    assert got["options"][0]["steer"] == "steer A"


def test_read_block_options_none_when_unset(tmp_path):
    seed_goal(tmp_path, "g1")
    assert GoalStore(tmp_path).read_block_options("g1") is None


def test_write_empty_block_options_overwrites_stale(tmp_path):
    # A re-block with no enumerable options must clear a prior menu, not keep it.
    seed_goal(tmp_path, "g1")
    store = GoalStore(tmp_path)
    store.write_block_options("g1", [BlockOption(key="a", label="A", steer="s")], "a")
    store.write_block_options("g1", [], "")
    got = store.read_block_options("g1")
    assert got["options"] == []
    assert got["recommended"] == ""
