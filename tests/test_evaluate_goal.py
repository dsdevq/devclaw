"""evaluate_goal: the on-demand direction-evaluation surface.

Exists as ``GoalService.evaluate_goal`` but only just got an ``@mcp.tool``
wrapper in ``server/tools.py``. These tests pin the service behavior the
wrapper relies on — verdict pass-through, corrections-to-inbox round-trip,
unknown-goal error — so future refactors can't quietly break the L1 contract
ops-agent depends on (see lifekit-stack PR #77)."""
from __future__ import annotations

import json

import pytest

from devclaw.goal.models import GoalStatus
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import FakeClaude, seed_goal


@pytest.fixture()
def db(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _svc(tmp_path, db):
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir, notify_url="", tick_seconds=900,
        eval_every=3, verify_done=False,
    )
    queue = TaskQueue(db)
    return GoalService(queue, db, config=cfg), goals_dir


@pytest.mark.asyncio
async def test_evaluate_goal_returns_verdict_on_track(tmp_path, db):
    """A well-formed evaluator response flows through verbatim to the caller."""
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="idle"))
    svc._goal_store.append_delivery("g", "add /health", "shipped: PR #1; gate green")

    response = json.dumps({
        "verdict": "on_track",
        "rationale": "delivery progressing toward done_when",
        "corrections": [],
    })
    svc._evaluator_caller = FakeClaude(response, role="evaluator")

    result = await svc.evaluate_goal("g")
    assert result["goal_id"] == "g"
    assert result["verdict"] == "on_track"
    assert "progressing" in result["rationale"]
    assert result["corrections"] == []


@pytest.mark.asyncio
async def test_evaluate_goal_appends_corrections_to_inbox(tmp_path, db):
    """An off_track verdict's corrections land in inbox.md as steering AND
    surface in the result — the L1 ops-agent loop relies on both."""
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="executing"))
    svc._goal_store.append_delivery("g", "wrong-direction work", "shipped X; not what done_when says")

    response = json.dumps({
        "verdict": "off_track",
        "rationale": "delivery drifted from done_when",
        "corrections": ["return to plan A", "drop feature X — out of scope"],
    })
    svc._evaluator_caller = FakeClaude(response, role="evaluator")

    result = await svc.evaluate_goal("g")
    assert result["verdict"] == "off_track"
    assert "return to plan A" in result["corrections"]
    assert "drop feature X" in " ".join(result["corrections"])
    inbox_text = (goals_dir / "g" / "inbox.md").read_text()
    assert "return to plan A" in inbox_text
    assert "drop feature X" in inbox_text
    # auto-eval source marker — distinguishes ops/evaluator corrections from
    # Denys-supplied steerings (trend detection's H4 filters on this).
    assert "auto-eval" in inbox_text


@pytest.mark.asyncio
async def test_evaluate_goal_unknown_id_raises(tmp_path, db):
    svc, _ = _svc(tmp_path, db)
    with pytest.raises(KeyError):
        await svc.evaluate_goal("nonexistent-goal")


@pytest.mark.asyncio
async def test_evaluate_goal_needs_human_returns_question(tmp_path, db):
    """needs_human verdicts surface the question — the operator-facing prompt."""
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    svc._goal_store.save_status("g", GoalStatus(phase="executing"))
    svc._goal_store.append_delivery("g", "ambiguous work", "shipped Y; unclear if right")

    response = json.dumps({
        "verdict": "needs_human",
        "rationale": "ambiguous direction",
        "corrections": [],
        "question": "Should we keep extending Y or pivot to Z?",
    })
    svc._evaluator_caller = FakeClaude(response, role="evaluator")

    result = await svc.evaluate_goal("g")
    assert result["verdict"] == "needs_human"
    assert "pivot to Z" in result["question"]
