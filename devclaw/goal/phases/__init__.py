"""Phase-handler pattern — one class per goal-lifecycle phase, tick is the dispatcher.

Option A from `~/memory/projects/devclaw/proposals/2026-06-27-module-boundaries.md`.
The protocol is intentionally narrow: each handler owns its own cognition,
state-machine, persistence, and notifications. Tick asks the registry for a
handler matching the current lifecycle, calls ``run``, and translates the
returned :class:`PhaseResult` back into its own ``Outcome`` enum.

``PhaseResult.outcome`` is a STRING (not the ``Outcome`` enum) so this module
can stay leaf-level: ``tick.py`` imports ``phases``, never the other way.
Strings line up 1:1 with ``Outcome`` values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..models import Goal, GoalStatus
    from ..store import GoalStore
    from ..tick import TickContext


@dataclass(frozen=True)
class PhaseResult:
    """The terminal value of one handler invocation.

    ``outcome`` is the string value of an ``Outcome`` member (e.g. ``"blocked"``,
    ``"advanced"``, ``"dispatched"``); tick maps it back to the enum. ``note`` is
    optional human context for the log (handlers also write to log + notify
    directly, so this is just a hand-off summary)."""

    outcome: str
    note: str = ""


class PhaseHandler(Protocol):
    """One handler per phase. ``name`` is the registry key (matches a lifecycle
    string). ``can_run`` is a cheap gate the dispatcher calls before ``run`` —
    use it to short-circuit when the phase shouldn't fire this tick (a blocked
    firming, for example, should not re-cognize until ``answer_unknowns`` lands).
    ``run`` does the work and returns the outcome string."""

    name: str

    async def can_run(
        self, goal: "Goal", status: "GoalStatus", store: "GoalStore"
    ) -> bool: ...

    async def run(
        self, goal_id: str, goal: "Goal", status: "GoalStatus", ctx: "TickContext"
    ) -> PhaseResult: ...
