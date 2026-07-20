"""The console's goal-detail feed carries a usage rollup — "what did this
goal cost" as cognition (host one-shot calls, from trace totals) plus worker
(per-task ``usage`` blocks the runner records into result_json). Pure reads,
best-effort: an unreadable store degrades the block to null, never a 500.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

import devclaw.server.http as http_mod
from devclaw.goal.phases import registry
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import FakeClaude, RecordingNotifier, seed_goal


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


def _get(path_params):
    scope = {"type": "http", "method": "GET", "path_params": path_params, "headers": []}
    return Request(scope)


def _land_task_with_usage(db, *, task_id, goal_id, usage):
    db.create_task(
        id=task_id, kind="implement_feature", workspace_dir="/w",
        goal="g", parent_goal_id=goal_id,
    )
    result = {"status": "ok"}
    if usage is not None:
        result["usage"] = usage
    db.mark_done(task_id, json.dumps(result))


def test_goal_json_usage_rolls_up_cognition_and_worker_tokens(tmp_path, db, monkeypatch):
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    monkeypatch.setattr(http_mod, "goals", svc)
    monkeypatch.setattr(http_mod, "store", db)

    # Two worker tasks report usage; a legacy task without a usage block must
    # count toward nothing (absence is normal, not an error).
    _land_task_with_usage(
        db, task_id="t1", goal_id="g",
        usage={"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 50, "cost_usd": 0.0},
    )
    _land_task_with_usage(
        db, task_id="t2", goal_id="g",
        usage={"input_tokens": 500, "output_tokens": 300, "cache_read_tokens": 0, "cost_usd": 0.25},
    )
    _land_task_with_usage(db, task_id="t3", goal_id="g", usage=None)
    # One cognition call with REAL usage from the CLI envelope.
    db.append_trace_event(
        trace_id="tr1", goal_id="g", kind="cognition",
        payload={"kind": "cognition", "role": "goal_planner",
                 "tokens_in": 700, "tokens_out": 90, "cost_usd": 0.1},
    )

    resp = asyncio.run(http_mod.goal_json(_get({"goal_id": "g"})))
    usage = json.loads(resp.body)["usage"]

    assert usage["workerInputTokens"] == 1500
    assert usage["workerOutputTokens"] == 500
    assert usage["tasksWithUsage"] == 2
    assert usage["workerCostUsd"] == pytest.approx(0.25)
    assert usage["cognitionTokensIn"] == 700
    assert usage["cognitionTokensOut"] == 90
    assert usage["cognitionCostUsd"] == pytest.approx(0.1)
    assert usage["totalTokens"] == 1500 + 500 + 700 + 90
    assert usage["totalCostUsd"] == pytest.approx(0.35)


def test_goal_json_usage_degrades_to_null_never_500(tmp_path, db, monkeypatch):
    svc, goals_dir = _svc(tmp_path, db)
    seed_goal(goals_dir, "g")
    monkeypatch.setattr(http_mod, "goals", svc)
    monkeypatch.setattr(http_mod, "store", db)

    def _boom(**kwargs):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(db, "trace_totals", _boom)

    resp = asyncio.run(http_mod.goal_json(_get({"goal_id": "g"})))
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["usage"] is None
