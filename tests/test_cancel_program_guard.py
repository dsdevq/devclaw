"""MCP ``cancel_program`` guards goal-owned programs — the ADR 0003 hierarchy.

``cancel_goal`` cascades DOWN to its child program (``service.cancel_goal`` →
``queue.cancel_program``). But cancelling a goal-owned program DIRECTLY does not
cascade UP: it would kill the program and leave the owning goal executing,
desynced from its dead program (the tick then reconciles a loss it never chose).
So the tool now REJECTS a program with a ``parent_goal_id`` and redirects to
``cancel_goal``. Standalone programs (``parent_goal_id is None`` — legacy raw
``start_program`` builds) still cancel normally, so back-compat is preserved.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from devclaw.server import tools as _tools
from devclaw.state_store import StateStore


class _SpyQueue:
    """Records cancel_program calls so a test can prove the guard short-circuits
    BEFORE the queue is ever touched (no partial cancellation, no desync)."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_program(self, program_id: str) -> bool:
        self.cancelled.append(program_id)
        return True


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def spy_queue(store, monkeypatch):
    spy = _SpyQueue()
    monkeypatch.setattr(_tools, "store", store)
    monkeypatch.setattr(_tools, "queue", spy)
    return spy


async def test_cancel_program_rejects_goal_owned_program_and_redirects_to_cancel_goal(
    store, spy_queue
):
    store.create_program(
        id="p1", goal="g", workspace_dir="/ws", parent_goal_id="my-goal"
    )

    with pytest.raises(ToolError) as exc:
        await _tools.cancel_program("p1")

    msg = str(exc.value)
    assert "cancel_goal" in msg        # tells the operator the right verb
    assert "my-goal" in msg            # names the owning goal to cancel
    # The guard fires before the queue — nothing is torn down, so the goal and
    # its program can never disagree via this path.
    assert spy_queue.cancelled == []


async def test_cancel_program_still_cancels_standalone_program(store, spy_queue):
    store.create_program(
        id="p2", goal="g", workspace_dir="/ws", parent_goal_id=None
    )

    out = json.loads(await _tools.cancel_program("p2"))

    assert out["program_id"] == "p2"
    assert out["cancelled"] is True
    assert spy_queue.cancelled == ["p2"]   # back-compat: it went through


async def test_cancel_program_unknown_id_still_errors(store, spy_queue):
    with pytest.raises(ToolError):
        await _tools.cancel_program("nope")
    assert spy_queue.cancelled == []
