"""The console's goal-detail feed carries STRUCTURED firming unknowns.

The firming model already emits ``options`` (and a documentation-only
``default_if_no_answer``) per unknown, and ``answer_unknowns`` round-trips
them — but the console JSON route used to strip both, so the Answer modal
could only ever render a bare textarea. These pin the passthrough end-to-end:
``/goals/{id}.json`` exposes each unknown's options + suggested default so a
blocked goal can be answered with one tap, and ``get_goal`` surfaces
``blocked_kind`` (previously absent, leaving the console's ``blockedKind``
permanently empty).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

import devclaw.server.http as http_mod
from devclaw.goal.firmed import parse_firmed
from devclaw.goal.models import GoalStatus
from devclaw.goal.phases import registry
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import FakeClaude, RecordingNotifier, seed_goal


DRAFT_WITH_OPTIONED_UNKNOWN = """\
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
    default_if_no_answer: calendar_month
  - id: cf-u2
    question: Anything else to exclude?
    why: Free-form.
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
    svc._goal_store.write_firmed_draft(goal_id, parse_firmed(DRAFT_WITH_OPTIONED_UNKNOWN))
    svc._goal_store.save_status(
        goal_id,
        GoalStatus(lifecycle="firming", phase="blocked",
                   blocked_on="2 questions", blocked_kind="needs_answer"),
    )


def _get(path_params):
    scope = {"type": "http", "method": "GET", "path_params": path_params, "headers": []}
    return Request(scope)


def test_get_goal_surfaces_blocked_kind(tmp_path, db):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)

    payload = svc.get_goal("g")
    assert payload["blocked_kind"] == "needs_answer"


def test_goal_json_unknowns_carry_options_for_one_tap_answers(tmp_path, db, monkeypatch):
    svc, goals_dir = _svc(tmp_path, db)
    _seed_blocked_firming(svc, goals_dir)
    monkeypatch.setattr(http_mod, "goals", svc)
    monkeypatch.setattr(http_mod, "store", db)

    resp = asyncio.run(http_mod.goal_json(_get({"goal_id": "g"})))
    body = json.loads(resp.body)

    assert body["blockedKind"] == "needs_answer"
    by_id = {u["id"]: u for u in body["unknowns"]}
    assert by_id["cf-u1"]["options"] == ["calendar_month", "rolling_30d"]
    assert by_id["cf-u1"]["defaultIfNoAnswer"] == "calendar_month"
    # Free-form questions keep an explicit empty shape — the console falls
    # back to the textarea, it never guesses.
    assert by_id["cf-u2"]["options"] == []
    assert by_id["cf-u2"]["defaultIfNoAnswer"] is None
