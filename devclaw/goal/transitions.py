"""The goal-state transition choke point — Tranche 1/PR4.

Before this PR every phase/lifecycle/in_flight change was a bespoke
``replace(status, phase=..., lifecycle=..., ...)`` + ``save_status`` call
scattered across ``tick.py`` / ``service.py`` / ``phases/firming.py``, with no
mechanism stopping two writers (a tick's planner await racing a concurrent
``steer_goal``/``cancel_goal``) from both reading the same snapshot and one
clobbering the other's write — the stale-snapshot un-cancel class. This module
is the enum + legality table :class:`~devclaw.goal.store.GoalStore.transition`
validates against; it is deliberately dependency-free (imports ``models``
only) so it can be unit-tested in complete isolation from the store/tick wiring.

:class:`State` is a STORED projection of today's ``GoalStatus`` fields
(``phase`` / ``lifecycle`` / ``in_flight``) — :func:`derive_state` computes it
the same way ``tick._classify`` always has, so the enum never becomes a second
source of truth to keep in sync by hand. :class:`Event` names the domain
action a handler is performing (not the field mutation) so the :data:`LEGAL`
table reads as "what can this goal DO from here", not "what fields changed".
"""

from __future__ import annotations

from enum import Enum

from .models import GoalStatus


class State(str, Enum):
    """The goal's coarse machine state, derived from ``GoalStatus`` — never
    hand-constructed outside :func:`derive_state` / a legacy-row rehydrate."""

    INVESTIGATING_IDLE = "investigating_idle"
    DISCOVERY_IN_FLIGHT = "discovery_in_flight"
    FIRMING_IDLE = "firming_idle"
    FIRMING_BLOCKED = "firming_blocked"
    EXECUTING_IDLE = "executing_idle"
    ACTION_IN_FLIGHT = "action_in_flight"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class Event(str, Enum):
    """The domain action a handler is asking to perform. One event can map to
    different targets depending on the FROM state (e.g. ``RESOLVE_INVESTIGATION``
    lands on ``FIRMING_IDLE`` or ``EXECUTING_IDLE`` depending on whether the
    firming phase is enabled) — see :data:`LEGAL`."""

    DISPATCH_DISCOVERY = "dispatch_discovery"
    DISCOVERY_SETTLED = "discovery_settled"
    RESOLVE_INVESTIGATION = "resolve_investigation"
    FIRMING_ADVANCE = "firming_advance"
    FIRMING_NEEDS_ANSWERS = "firming_needs_answers"
    DISPATCH_ACTION = "dispatch_action"
    ACTION_SETTLED = "action_settled"
    OPEN_DONE_GATE = "open_done_gate"
    DONE_GATE_SETTLED = "done_gate_settled"
    RESUME_IDLE = "resume_idle"
    ACHIEVE = "achieve"
    BLOCK = "block"
    UNBLOCK = "unblock"
    CANCEL = "cancel"


def derive_state(status: GoalStatus) -> State:
    """TOTAL pure projection from today's ``phase``/``lifecycle``/``in_flight``
    fields — mirrors ``tick._classify``'s precedence exactly (see that
    function's docstring), with one addition ``_classify`` doesn't need:
    ``phase == "blocked"`` is checked BEFORE ``in_flight`` here, because a
    blocked goal MAY carry a preserved ``in_flight`` ref (the corrupt-doc /
    lost-ref block handlers deliberately keep it so the ref settles normally
    once the block clears) and blocked-ness must win for the STATE even
    though ``_classify`` itself still polls the ref first for its own
    (unrelated) dispatch purposes. Never raises — every field combination,
    including a legacy ``lifecycle=None`` row, maps to exactly one State."""
    if status.phase == "done":
        return State.DONE
    if status.phase == "cancelled":
        return State.CANCELLED
    if status.phase == "blocked":
        return State.FIRMING_BLOCKED if status.lifecycle == "firming" else State.BLOCKED
    if status.in_flight is not None:
        ref = status.in_flight
        if getattr(ref, "is_discovery", False):
            return State.DISCOVERY_IN_FLIGHT
        if getattr(ref, "is_done_check", False):
            return State.VERIFYING
        return State.ACTION_IN_FLIGHT
    lifecycle = status.lifecycle or "executing"
    if lifecycle == "investigating":
        return State.INVESTIGATING_IDLE
    if lifecycle == "firming":
        return State.FIRMING_IDLE
    return State.EXECUTING_IDLE


#: alias — some call sites read better as "the state of X" than "derive from X".
state_of = derive_state


#: (from_state, event) -> legal target states. Built by walking every
#: production write site in tick.py / service.py / phases/firming.py (Tranche
#: 1/PR4) — NOT a theoretical state machine. BLOCK and CANCEL are legal from
#: every non-terminal state (any handler can block or cancel a goal no matter
#: what it's doing); DONE/CANCELLED are terminal (no outgoing events).
#:
#: BLOCKED carries planning-family outgoing edges (DISPATCH_ACTION,
#: OPEN_DONE_GATE, RESUME_IDLE, ACHIEVE) because `_classify` routes a
#: phase=blocked+no-ref goal to EXECUTING, and `_handle_executing` still plans
#: when unread steering exists — so a blocked goal can dispatch, propose done,
#: sleep, or re-block, exactly like an executing_idle one. `BLOCKED,
#: DISPATCH_ACTION` also covers `tick._readopt_orphaned_ref` (PR7's startup
#: sweep; formerly the per-tick `_readopt_orphaned_program`) re-adopting a
#: lost ref from a blocked goal.
LEGAL: dict[tuple[State, Event], frozenset[State]] = {
    (State.INVESTIGATING_IDLE, Event.DISPATCH_DISCOVERY): frozenset({State.DISCOVERY_IN_FLIGHT}),
    (State.INVESTIGATING_IDLE, Event.RESOLVE_INVESTIGATION): frozenset(
        {State.FIRMING_IDLE, State.EXECUTING_IDLE}
    ),
    (State.INVESTIGATING_IDLE, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.INVESTIGATING_IDLE, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.DISCOVERY_IN_FLIGHT, Event.DISCOVERY_SETTLED): frozenset({State.INVESTIGATING_IDLE}),
    (State.DISCOVERY_IN_FLIGHT, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.DISCOVERY_IN_FLIGHT, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.FIRMING_IDLE, Event.FIRMING_ADVANCE): frozenset({State.EXECUTING_IDLE}),
    (State.FIRMING_IDLE, Event.FIRMING_NEEDS_ANSWERS): frozenset({State.FIRMING_BLOCKED}),
    (State.FIRMING_IDLE, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.FIRMING_IDLE, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.FIRMING_BLOCKED, Event.FIRMING_ADVANCE): frozenset({State.EXECUTING_IDLE}),
    (State.FIRMING_BLOCKED, Event.FIRMING_NEEDS_ANSWERS): frozenset({State.FIRMING_BLOCKED}),
    (State.FIRMING_BLOCKED, Event.UNBLOCK): frozenset({State.FIRMING_IDLE}),
    (State.FIRMING_BLOCKED, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.FIRMING_BLOCKED, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.EXECUTING_IDLE, Event.DISPATCH_ACTION): frozenset({State.ACTION_IN_FLIGHT}),
    (State.EXECUTING_IDLE, Event.OPEN_DONE_GATE): frozenset({State.VERIFYING}),
    (State.EXECUTING_IDLE, Event.RESUME_IDLE): frozenset({State.EXECUTING_IDLE}),
    (State.EXECUTING_IDLE, Event.ACHIEVE): frozenset({State.DONE}),
    (State.EXECUTING_IDLE, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.EXECUTING_IDLE, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.ACTION_IN_FLIGHT, Event.ACTION_SETTLED): frozenset({State.EXECUTING_IDLE}),
    (State.ACTION_IN_FLIGHT, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.ACTION_IN_FLIGHT, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.VERIFYING, Event.DONE_GATE_SETTLED): frozenset({State.EXECUTING_IDLE}),
    (State.VERIFYING, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.VERIFYING, Event.CANCEL): frozenset({State.CANCELLED}),
    (State.BLOCKED, Event.UNBLOCK): frozenset({State.EXECUTING_IDLE}),
    (State.BLOCKED, Event.DISPATCH_ACTION): frozenset({State.ACTION_IN_FLIGHT}),
    (State.BLOCKED, Event.OPEN_DONE_GATE): frozenset({State.VERIFYING}),
    (State.BLOCKED, Event.RESUME_IDLE): frozenset({State.EXECUTING_IDLE}),
    (State.BLOCKED, Event.ACHIEVE): frozenset({State.DONE}),
    (State.BLOCKED, Event.BLOCK): frozenset({State.BLOCKED}),
    (State.BLOCKED, Event.CANCEL): frozenset({State.CANCELLED}),
    # DONE, CANCELLED: no outgoing events — terminal, intentionally absent.
}


class IllegalTransition(RuntimeError):
    """A handler proposed an ``(event, target)`` pair the LEGAL table doesn't
    permit from the goal's CURRENT stored state. Always a bug (a handler
    computing the wrong event for its branch, or the LEGAL table missing a
    real code path) — never an expected race; see :class:`TransitionConflict`
    for that. tick_goal's choke-point catch force-blocks the goal and pings
    the owner once rather than let the tick loop crash-retry forever."""

    def __init__(self, goal_id: str, from_state: State, event: Event, target: State) -> None:
        self.goal_id = goal_id
        self.from_state = from_state
        self.event = event
        self.target = target
        super().__init__(
            f"goal {goal_id!r}: {event.value} ({from_state.value} → {target.value}) "
            "is not a legal transition"
        )


class TransitionConflict(RuntimeError):
    """The stored ``(state, version)`` no longer matches what the caller's
    write was based on — another writer (a steer_goal/cancel_goal call landing
    mid-tick, most commonly) committed in between the caller's load and this
    transition. Expected, not a bug: tick_goal's choke-point catch abandons
    the tick's write (``Outcome.CONFLICT``) and lets the NEXT tick read the
    fresh state rather than clobber it — the fix for the stale-snapshot
    un-cancel class."""

    def __init__(
        self,
        goal_id: str,
        *,
        expected: "tuple[State, int]",
        found: "tuple[State, int]",
    ) -> None:
        self.goal_id = goal_id
        self.expected = expected
        self.found = found
        super().__init__(
            f"goal {goal_id!r}: expected {expected[0].value} v{expected[1]}, "
            f"found {found[0].value} v{found[1]} — write abandoned"
        )
