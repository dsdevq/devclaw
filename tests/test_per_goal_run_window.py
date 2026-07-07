"""Per-goal run-window at the heartbeat: a goal outside its OWN window is skipped
(0 tokens for it) while every other goal ticks normally — the loop-level half of
the mechanism unit-tested in test_dispatch_gate.py.

Confines a token-heavy standing goal (e.g. a CloseLoop ownership loop) to nights
without gating the rest of the engine, which the global run-window can't do."""
from __future__ import annotations

import json

from devclaw.goal.tick import Outcome, tick_all
from devclaw.goal.store import GoalStore
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal

ACT = json.dumps(
    {"decision": "act", "note": "ship next",
     "actions": [{"tool": "start_program", "goal": "build /health"}]}
)


class WindowEngine(FakeEngine):
    """FakeEngine (global gates read open via getattr) plus a per-goal window gate
    driven by an explicit blocked-set, so the test controls which goals are
    outside their window without touching the wall clock."""

    def __init__(self, blocked: set[str], **kw) -> None:
        super().__init__(**kw)
        self._blocked = set(blocked)

    def goal_operator_block(self, goal_id: str, now_ms: int) -> tuple[bool, str]:
        return (True, "outside run window") if goal_id in self._blocked else (False, "")


async def test_windowed_out_goal_is_skipped_others_tick(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "day")
    seed_goal(tmp_path, "night")
    planner, evaluator = FakeClaude(ACT), FakeClaude()
    engine = WindowEngine({"night"})

    out = await tick_all(
        store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=RecordingNotifier(), prepare_ws=fake_prepare, eval_every=99,
    )

    assert out["night"] is Outcome.RATE_LIMITED   # outside its window → held
    assert out["day"] is Outcome.DISPATCHED       # inside → ticked normally
    assert planner.calls == 1                     # only the day goal planned — 0 tokens for night
    # exactly one dispatch, and it was the day goal
    assert len(engine.dispatched) == 1
    _, goal, _ = engine.dispatched[0]
    assert goal.workspace_dir == "/repos/demo"
