"""RUN_SUMMARY.md — the at-a-glance close-out artifact at the ACHIEVE close.

A finished goal used to leave only scattered evidence (log lines, delivery
sections, trace rows); nothing answered "what did this run actually do" in
one read. The close now renders a projection of the goal's own rows —
delivery traces (gate/PR/diff stats), cognition totals, phase-history
duration, checklist progress — as a generated view next to STATUS.md, and
rides a compact line on the owner's goal-complete ping. Best-effort: a
summary hiccup never disturbs a verified close.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal.models import Checklist, ChecklistItem, GoalStatus, InFlight, PollResult
from devclaw.goal.run_summary import build_run_summary
from devclaw.goal.tick import Outcome
from tests.goal_fakes import (
    Clock,
    FakeClaude,
    FakeEngine,
    RecordingNotifier,
    fake_prepare,
    seed_goal,
)

@pytest.fixture(autouse=True)
def _no_deploys(monkeypatch):
    """Same stub as test_goal_tick.py: the achieved close triggers the
    best-effort auto-deploy, which must never spawn real docker under test."""
    from devclaw.delivery import deploy as deploy_mod

    async def _no_deploy(workspace_dir, slug):
        raise RuntimeError("no deploys under test")

    monkeypatch.setattr(deploy_mod, "deploy_project", _no_deploy)


# ---- the pure renderer ------------------------------------------------------


def _delivery_trace(label, *, gate=True, pr="", files=None, ins=None, dels=None):
    return {
        "kind": "delivery",
        "payload": {
            "action_label": label, "gate_passed": gate, "pr_url": pr,
            "diff_files": files, "diff_insertions": ins, "diff_deletions": dels,
        },
    }


def test_run_summary_aggregates_deliveries_prs_diff_and_tokens():
    status = GoalStatus(
        phase="done",
        last_eval_note="all clauses pass",
        phase_history=(
            {"phase": "idle", "at": "2026-07-01T10:00:00+00:00"},
            {"phase": "done", "at": "2026-07-03T14:30:00+00:00"},
        ),
    )
    traces = [
        _delivery_trace("task a", gate=True, pr="https://x/pr/1", files=3, ins=100, dels=20),
        _delivery_trace("task b", gate=False),
        _delivery_trace("task c", gate=True, pr="https://x/pr/2", files=2, ins=50, dels=5),
        {"kind": "cognition", "payload": {}},  # non-delivery rows are ignored
    ]
    totals = {
        "events_by_kind": {"cognition": 12, "delivery": 3},
        "cognition_tokens_in": 900_000, "cognition_tokens_out": 300_000,
        "cognition_cost_usd": 4.2,
    }
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e", status="done"),
        ChecklistItem(id="b", requirement="r", evidence_target="e", status="done"),
        ChecklistItem(id="c", requirement="r", evidence_target="e", status="blocked"),
    ])

    md, compact = build_run_summary("g1", status, traces, totals=totals,
                                    checklist=cl, objective="ship the thing")

    # compact line: everything at a glance
    assert "3 deliveries" in compact
    assert "2 PRs" in compact
    assert "+150/-25 across 5 files" in compact
    assert "1.2M tokens ($4.20)" in compact
    assert "2d 4h" in compact
    # markdown: sections + rows
    assert "# g1 — run summary" in md
    assert "**Objective:** ship the thing" in md
    assert "2 gate-passed, 1 gate-failed" in md
    assert "2/3 checklist items done (1 blocked)" in md
    assert "https://x/pr/1" in md and "https://x/pr/2" in md
    assert "task b · gate=FAILED" in md
    assert "all clauses pass" in md
    assert "over 12 cognition calls" in md


def test_run_summary_degrades_on_empty_rows():
    # no traces, no totals, no checklist, no history → still renders; the
    # compact line stays honest (no fake zeros beyond the delivery count)
    md, compact = build_run_summary("g1", GoalStatus(phase="done"), [])
    assert compact == "0 deliveries"
    assert "# g1 — run summary" in md
    assert "PRIOR" not in md
    assert "tokens" not in compact
    # malformed trace payloads are skipped, never a crash
    md2, compact2 = build_run_summary(
        "g1", GoalStatus(phase="done"),
        [{"kind": "delivery", "payload": "not-a-dict"}, "junk", {"kind": "delivery"}],
    )
    assert compact2 == "0 deliveries"


# ---- the achieve-path integration -------------------------------------------


@pytest.mark.asyncio
async def test_achieved_close_writes_run_summary_view_and_notifies(tmp_path, monkeypatch):
    from devclaw.goal.store import GoalStore
    from devclaw.goal.tick import tick_goal

    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify",
                           is_done_check=True),
    ))
    evaluator = FakeClaude(json.dumps({
        "verdict": "achieved", "rationale": "done_when satisfied",
        "clauses": [{"clause": "c", "satisfied": True, "evidence": "e"}],
    }))
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="report"))
    notifier = RecordingNotifier()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(role="planner"), evaluator_caller=evaluator,
        notifier=notifier, prepare_ws=fake_prepare, verify_done=True,
    )

    assert out is Outcome.DONE
    # the view exists and is a rendered projection
    summary = (tmp_path / "g" / "RUN_SUMMARY.md").read_text()
    assert "# g — run summary" in summary
    assert "Generated view" in summary
    # the owner ping carries the compact line
    done_msgs = [m for m in notifier.sent if "complete (verified)" in m]
    assert done_msgs and "deliveries" in done_msgs[0]


@pytest.mark.asyncio
async def test_summary_hiccup_never_disturbs_a_verified_close(tmp_path, monkeypatch):
    # fail-open for the CLOSE (never undo a verified goal): a crashing summary
    # renderer logs, skips the artifact, and the goal still closes + notifies.
    from devclaw.goal import run_summary as rs
    from devclaw.goal.store import GoalStore
    from devclaw.goal.tick import tick_goal

    def boom(*a, **k):
        raise RuntimeError("summary boom")

    monkeypatch.setattr(rs, "build_run_summary", boom)

    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "verify",
                           is_done_check=True),
    ))
    evaluator = FakeClaude(json.dumps({
        "verdict": "achieved", "rationale": "ok",
        "clauses": [{"clause": "c", "satisfied": True, "evidence": "e"}],
    }))
    notifier = RecordingNotifier()

    out = await tick_goal(
        "g", store=store,
        engine=FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="r")),
        planner_caller=FakeClaude(role="planner"), evaluator_caller=evaluator,
        notifier=notifier, prepare_ws=fake_prepare, verify_done=True,
    )

    assert out is Outcome.DONE
    assert store.load_status("g").phase == "done"
    assert not (tmp_path / "g" / "RUN_SUMMARY.md").exists()
    assert any("complete (verified)" in m for m in notifier.sent)
