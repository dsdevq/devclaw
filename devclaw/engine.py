"""The engine seam.

DevClaw's orchestration (task queue, planner, state store) drives an *Engine* —
the thing that actually executes one coding task in isolation and streams back
events. OpenHands-in-a-docker-sandbox is the only implementation today
(:func:`devclaw.sandcastle_runner.run_sandcastle`), but the orchestration
depends ONLY on this interface, never on OpenHands directly.

That's the "orchestration ⊥ engine" decoupling from the architecture: the
engine is pinned (OpenHands) but swappable behind one method, and the
orchestration stays testable with a stub engine (the tests inject one).

An Engine is any async callable ``(EngineRequest) -> EngineResult`` — a plain
async function satisfies it, which is why ``run_sandcastle`` is one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Union

from .state_store import TaskKind


@dataclass
class EngineEvent:
    """One observation streamed by the engine while a task runs. Mirrors the
    ``event:`` line shape the in-sandbox runner emits."""

    id: Optional[str]
    type: str
    source: str
    ts: Union[int, str]
    payload: object


@dataclass
class EngineRequest:
    """Inputs to execute one task. The same kinds the MCP tool surface exposes;
    the engine picks the right behavior per kind."""

    kind: TaskKind
    workspace_dir: str
    goal: str
    #: optional callback, invoked once per :class:`EngineEvent` the engine emits
    on_event: Optional[Callable[[EngineEvent], None]] = None


#: Terminal verdict from one task. ``status == "ok"`` carries
#: ``workspaceDir``/``message`` (+ ``agent_output`` for debugging);
#: ``status == "error"`` carries ``error`` (+ optional ``trace``).
EngineResult = dict


class Engine(Protocol):
    """Anything that can execute one task. A plain ``async def f(request)`` works."""

    async def __call__(self, request: EngineRequest) -> EngineResult: ...
