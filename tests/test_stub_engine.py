"""Stub engine + cognition tests (harness-validation mode)."""

import tempfile
from pathlib import Path

from devclaw.engine import EngineRequest
from devclaw.engine.stub import stub_engine, stub_goal_planner


async def test_stub_engine_builds_jyq_for_golden_goal():
    ws = tempfile.mkdtemp()
    events = []
    req = EngineRequest(
        kind="implement_feature", workspace_dir=ws,
        goal="Build the jyq CLI package (JSON<->YAML)", on_event=events.append,
    )
    res = await stub_engine(req)
    assert res["status"] == "ok"
    assert (Path(ws) / "jyq" / "__main__.py").exists()
    assert events and events[0].type == "StubBuildEvent"  # event path exercised


async def test_stub_engine_placeholder_for_unknown_goal():
    ws = tempfile.mkdtemp()
    res = await stub_engine(EngineRequest(kind="implement_feature", workspace_dir=ws, goal="build a spaceship"))
    assert res["status"] == "ok"
    assert (Path(ws) / "STUB_BUILD.txt").exists()
    assert not (Path(ws) / "jyq").exists()


async def test_stub_goal_planner_returns_one_task():
    tasks = await stub_goal_planner("fix the thing", "/ws")
    assert tasks[0].goal == "fix the thing"
