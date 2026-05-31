"""Stub engine + cognition tests (harness-validation mode)."""

import json
import tempfile
from pathlib import Path

from devclaw.engine import EngineRequest
from devclaw.stub_engine import stub_engine, stub_grill, stub_goal_planner, stub_spec_planner


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


async def test_stub_grill_asks_then_finalizes():
    # first turn: no transcript marker → asks
    first = json.loads(await stub_grill("PROJECT IDEA:\nbuild jyq\n\nDecide:"))
    assert first["action"] == "ask"
    # second turn: transcript present → finalizes a spec
    second = json.loads(await stub_grill("PROJECT IDEA:\nx\n\nINTERVIEW SO FAR:\n1. Q ...\n"))
    assert second["action"] == "done" and "jyq" in second["spec"]


async def test_stub_planners_return_dags():
    spec_tasks = await stub_spec_planner("# spec", "/ws")
    assert len(spec_tasks) == 1 and spec_tasks[0].milestone
    goal_tasks = await stub_goal_planner("fix the thing", "/ws")
    assert goal_tasks[0].goal == "fix the thing"
