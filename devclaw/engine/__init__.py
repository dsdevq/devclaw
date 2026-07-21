"""The engine seam.

DevClaw's orchestration (task queue, planner, state store) drives an *Engine* —
the thing that actually executes one coding task in isolation and streams back
events. OpenHands-in-a-docker-sandbox is the only implementation today
(:func:`devclaw.engine.sandcastle.run_sandcastle`), but the orchestration
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

from ..state_store import TaskKind


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
    #: optional verify gate — a shell command the engine runs in the workspace
    #: AFTER the agent finishes. Its exit code is the real definition of "done"
    #: (the agent's own "I'm finished" is not trusted). None → no gate.
    verify_cmd: Optional[str] = None
    #: optional per-task sandbox image (ADR 0005) — the owning project's
    #: ``sandbox_image`` override, resolved by the task queue at dispatch.
    #: None → the engine's own DEVCLAW_SANDBOX_IMAGE default. Docker-less
    #: engines (host, stub) ignore it.
    sandbox_image: Optional[str] = None
    #: optional owner-instance id — stamps the sandbox's ``devclaw.owner``
    #: label so the startup orphan sweep only ever reaps THIS instance's
    #: leftovers, never a concurrent devclaw process's live sandboxes on the
    #: same daemon. Docker-less engines (host, stub) ignore it.
    owner_id: Optional[str] = None


#: Terminal verdict from one task. ``status == "ok"`` carries
#: ``workspaceDir``/``message`` (+ ``agent_output`` for debugging);
#: ``status == "error"`` carries ``error`` (+ optional ``trace``);
#: ``status == "rate_limited"`` carries ``error`` + ``retry_after`` (the host
#: pauses-and-resumes); ``status == "blocked"`` carries ``reason`` — the worker's
#: honest self-report that it genuinely cannot finish (missing capability,
#: contradictory/impossible instructions). A ``blocked`` verdict is NOT an
#: approval: the host fails it CLOSED and does NOT retry (a re-run re-blocks
#: identically), surfacing the reason instead. When a ``verify_cmd`` ran,
#: ``result["verify"]`` carries the gate verdict
#: ``{ran, cmd, passed, exit_code, timed_out, output}`` — the orchestration
#: (not the engine) decides done-vs-failed from ``passed``.
EngineResult = dict


class Engine(Protocol):
    """Anything that can execute one task. A plain ``async def f(request)`` works."""

    async def __call__(self, request: EngineRequest) -> EngineResult: ...
