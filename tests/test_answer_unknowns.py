"""``GoalService.answer_unknowns`` — the synchronous owner-side entry for
firming round N>=2. Validates that the answer set matches the current
unknowns exactly (no partials, no extras), then delegates to FirmingHandler."""

from __future__ import annotations

import pytest

from devclaw.goal.firmed import parse_firmed
from devclaw.goal.models import GoalStatus
from devclaw.goal.phases import registry
from devclaw.goal.phases.firming import FirmingHandler
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import FakeClaude, RecordingNotifier, seed_goal


DRAFT_WITH_UNKNOWNS = """\
status: needs_owner_answers
round: 1
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns:
  - id: cf-u1
    question: Period model — calendar month or rolling 30d?
    why: No reporting framework.
    options: [calendar_month, rolling_30d]
"""


DRAFT_FIRMED = """\
status: firmed
round: 2
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns: []
"""


@pytest.fixture
def db(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def reset_phase_registry():
    yield
    registry.reset()


def _svc(tmp_path, db):
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir,
        notify_url="",
        tick_seconds=900,
        eval_every=3,
        verify_done=False,
    )
    queue = TaskQueue(db)
    svc = GoalService(
        queue, db, config=cfg,
        notifier=RecordingNotifier(),
        planner_caller=FakeClaude(role="goal_planner"),
        evaluator_caller=FakeClaude(role="goal_evaluator"),
    )
    return svc, goals_dir


def _seed_blocked_firming(svc, goals_dir, goal_id="g"):
    seed_goal(goals_dir, goal_id)
    svc._goal_store.write_firmed_draft(goal_id, parse_firmed(DRAFT_WITH_UNKNOWNS))
    svc._goal_store.save_status(
        goal_id, GoalStatus(lifecycle="firming", phase="blocked",
                            blocked_on="1 question"),
    )


@pytest.mark.asyncio
async def test_complete_answers_advance_to_firmed(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)
    registry.register(
        "firming",
        FirmingHandler(caller=FakeClaude(DRAFT_FIRMED, role="goal_firming")),
    )

    result = await svc.answer_unknowns("g", {"cf-u1": "calendar_month"})

    assert result["status"] == "firmed"
    assert result["round"] == 2
    assert svc._goal_store.load_status("g").lifecycle == "executing"


@pytest.mark.asyncio
async def test_missing_answer_rejected(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)
    registry.register(
        "firming",
        FirmingHandler(caller=FakeClaude(DRAFT_FIRMED, role="goal_firming")),
    )

    with pytest.raises(ValueError) as exc:
        await svc.answer_unknowns("g", {"other-id": "x"})
    assert "missing" in str(exc.value)
    # the goal stays blocked — no half-firm
    assert svc._goal_store.load_status("g").phase == "blocked"


@pytest.mark.asyncio
async def test_extra_answer_rejected(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)

    with pytest.raises(ValueError) as exc:
        await svc.answer_unknowns(
            "g", {"cf-u1": "calendar_month", "cf-uX": "garbage"},
        )
    assert "extra" in str(exc.value)


@pytest.mark.asyncio
async def test_no_draft_yet_rejected(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")  # no firmed-draft on disk

    with pytest.raises(ValueError):
        await svc.answer_unknowns("g", {"cf-u1": "calendar_month"})


def test_get_goal_surfaces_firmed_draft(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)

    payload = svc.get_goal("g")
    assert payload["firmed_draft"] is not None
    assert payload["firmed_draft"]["status"] == "needs_owner_answers"
    assert [u["id"] for u in payload["firmed_draft"]["unknowns"]] == ["cf-u1"]
