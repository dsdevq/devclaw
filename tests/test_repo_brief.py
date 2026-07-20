"""Repo-scoped worker memory brief (mission-control borrow item 3).

goal_docs die with their goal and the sandbox workspace is git clean -fdx-
wiped per dispatch, so every new goal on the same repo relearned build
quirks from zero. These pin the host-side loop that fixes that:

- the pure merge policy (line dedupe + size cap, zero LLM);
- the project_docs row keyed by NORMALIZED workspace path (outlives goals);
- settle folds a worker's REPO NOTES hand-back into the brief, best-effort;
- the NEXT dispatch on the same workspace prepends the brief to the goal
  text — plain text injection, model-agnostic — while read-only reviews
  stay unseeded and idle ticks stay zero-token.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal import repo_brief
from devclaw.goal.models import GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_goal
from tests.goal_fakes import (
    Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal,
)

ACT_FEATURE = json.dumps(
    {"decision": "act", "note": "feat",
     "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}]}
)
ACT_REVIEW = json.dumps(
    {"decision": "act", "note": "verify",
     "actions": [{"tool": "review_repository", "goal": "verify the delivery"}]}
)
SLEEP = json.dumps({"decision": "sleep", "note": "waiting"})


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


async def _tick(store, goal_id, planner, engine):
    return await tick_goal(
        goal_id, store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(),
        notifier=RecordingNotifier(), notify_url="http://relay",
        prepare_ws=fake_prepare, eval_every=99, verify_done=True,
    )


# ---- the pure merge policy --------------------------------------------------


def test_merge_repo_notes_dedupes_and_appends():
    existing = "npm test needs NODE_OPTIONS=--max-old-space-size=4096"
    merged = repo_brief.merge_repo_notes(
        existing,
        "- npm test needs NODE_OPTIONS=--max-old-space-size=4096\n"
        "- e2e suite requires the dev server on :4200",
    )
    assert merged.splitlines() == [
        "npm test needs NODE_OPTIONS=--max-old-space-size=4096",
        "e2e suite requires the dev server on :4200",
    ]


def test_merge_repo_notes_skips_none_and_empty_lines():
    assert repo_brief.merge_repo_notes(None, "none") == ""
    assert repo_brief.merge_repo_notes("", "  \n- none\n") == ""


def test_merge_repo_notes_cap_drops_oldest_first():
    existing = "\n".join(f"old fact {i} " + "x" * 90 for i in range(50))
    merged = repo_brief.merge_repo_notes(existing, "the newest fact")
    assert len(merged) <= repo_brief.MAX_BRIEF_CHARS
    assert merged.splitlines()[-1] == "the newest fact"
    assert "old fact 0" not in merged  # oldest fell off, newest survived


def test_scope_key_normalizes_like_the_registry():
    assert repo_brief.scope_key_for("/repos/demo/") == "/repos/demo"
    assert repo_brief.scope_key_for("/repos//demo") == "/repos/demo"
    assert repo_brief.scope_key_for(None) is None
    assert repo_brief.scope_key_for("  ") is None


def test_render_brief_prefix_empty_for_blank_brief():
    assert repo_brief.render_brief_prefix(None) == ""
    assert repo_brief.render_brief_prefix("  \n ") == ""
    prefix = repo_brief.render_brief_prefix("fact one")
    assert "fact one" in prefix
    assert prefix.endswith("---\n\n")


# ---- the store row (project-scoped, outlives goals) -------------------------


def test_repo_brief_round_trips_and_outlives_goals(tmp_path):
    store = _store(tmp_path)
    assert store.read_repo_brief("/repos/demo") == ""
    store.write_repo_brief("/repos/demo", "fact")
    assert store.read_repo_brief("/repos/demo") == "fact"
    # Keyed by workspace, not goal — a different scope reads empty.
    assert store.read_repo_brief("/repos/other") == ""


# ---- settle writeback --------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_folds_worker_repo_notes_into_the_brief(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # workspace_dir=/repos/demo
    store.save_status(
        "g", GoalStatus(
            phase="in_flight",
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health",
        pr_url="https://github.com/o/r/pull/9", gate_passed=True,
        repo_notes="tests need `npm run test:ci`, not `npm test`; build is pnpm-only",
    ))

    await _tick(store, "g", FakeClaude(SLEEP), engine)

    brief = store.read_repo_brief("/repos/demo")
    assert "tests need `npm run test:ci`" in brief


@pytest.mark.asyncio
async def test_settle_without_repo_notes_writes_nothing(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight",
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="added /health", gate_passed=True,
    ))

    await _tick(store, "g", FakeClaude(SLEEP), engine)

    assert store.read_repo_brief("/repos/demo") == ""


@pytest.mark.asyncio
async def test_repo_notes_writeback_failure_never_wedges_the_settle(tmp_path, monkeypatch):
    """The brief is cross-goal hint material — a store hiccup on the writeback
    must not fail the settle or leave the ref in flight (loud-failure applies
    to the SETTLE; the notes are best-effort by contract)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status(
        "g", GoalStatus(
            phase="in_flight",
            in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "add /health"),
        ),
    )
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="ok", gate_passed=True,
        repo_notes="a fact",
    ))
    monkeypatch.setattr(
        store, "write_repo_brief",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    await _tick(store, "g", FakeClaude(SLEEP), engine)

    assert store.load_status("g").in_flight is None  # settled despite the hiccup


# ---- dispatch injection ------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_prepends_the_repo_brief_to_the_goal_text(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # no STATUS yet → plan → dispatch
    store.write_repo_brief("/repos/demo", "build is pnpm-only")
    engine = FakeEngine()

    out = await _tick(store, "g", FakeClaude(ACT_FEATURE), engine)

    assert out is Outcome.DISPATCHED
    dispatched_goal = engine.dispatched[0][0].goal
    assert dispatched_goal.startswith("[Repo notes")
    assert "build is pnpm-only" in dispatched_goal
    assert dispatched_goal.rstrip().endswith("add /health")


@pytest.mark.asyncio
async def test_review_dispatch_stays_unseeded_by_the_brief(tmp_path):
    """A read-only review grounds the evaluator — seeding it with prior
    workers' claims would bias the very reality-check the loop leans on."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_repo_brief("/repos/demo", "build is pnpm-only")
    engine = FakeEngine()

    await _tick(store, "g", FakeClaude(ACT_REVIEW), engine)

    dispatched_goal = engine.dispatched[0][0].goal
    assert "Repo notes" not in dispatched_goal
    assert "pnpm-only" not in dispatched_goal


@pytest.mark.asyncio
async def test_empty_brief_leaves_the_goal_text_byte_identical(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    engine = FakeEngine()

    await _tick(store, "g", FakeClaude(ACT_FEATURE), engine)

    assert engine.dispatched[0][0].goal == "add /health"


@pytest.mark.asyncio
async def test_delivery_record_stays_clean_of_the_repo_brief(tmp_path):
    """The brief is worker INPUT, not evidence: the settled delivery record
    (→ the direction evaluator's "grounded deliveries" section) must carry the
    clean action text, never the prepended prior-run hints — otherwise every
    delivery re-presents unverified claims as shipped grounding
    (invariant-guard finding on this PR)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_repo_brief("/repos/demo", "build is pnpm-only")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", detail="ok", gate_passed=True,
    ))

    await _tick(store, "g", FakeClaude(ACT_FEATURE), engine)   # dispatch (prefixed)
    await _tick(store, "g", FakeClaude(SLEEP), engine)         # settle

    deliveries = store.recent_deliveries("g")
    assert "add /health" in deliveries
    assert "[Repo notes" not in deliveries
    assert "pnpm-only" not in deliveries


@pytest.mark.asyncio
async def test_idle_tick_stays_zero_token_with_a_brief_present(tmp_path):
    """The brief read happens ONLY on the dispatch path — an idle tick must
    not gain a read, an LLM call, or any other work (the quota guardrail)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", cadence="1d")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    store.write_repo_brief("/repos/demo", "a fact")
    planner, engine = FakeClaude(ACT_FEATURE), FakeEngine()

    out = await _tick(store, "g", planner, engine)

    assert out is Outcome.IDLE
    assert planner.calls == 0
    assert engine.dispatched == []
