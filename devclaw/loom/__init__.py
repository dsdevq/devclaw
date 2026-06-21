"""loom — the reusable orchestration core, sans the ``-claw`` prefix.

This package is the **extraction seam**: the engine-agnostic substrate devclaw is
built on, grouped under a neutral name so it can eventually become a standalone,
reusable library (an orchestrator weaving many threads — goals, tasks, agents —
into delivered work). devclaw remains the concrete product (the MCP server, the
OpenHands engine, the GitHub delivery); loom is the part with no opinion about
*which* engine or product uses it.

What lives here today (physically): the pure, self-contained utilities —
:mod:`~devclaw.loom.limits` (the usage-limit/rate-limit failure classifier) and
:mod:`~devclaw.loom.test_integrity` (the gate's deleted/weakened-test guard).
What is re-exported here (the curated public surface, pending a physical move as
the coupled cores are proven through a second consumer): the goal domain types
and the durable on-disk store. Old import paths (``devclaw.limits`` etc.) keep
working via thin shims, so this extraction is reversible and non-breaking.

Import the core from one place::

    from devclaw.loom import classify_failure, scan_diff, Goal, GoalStore
"""

from __future__ import annotations

# --- physically owned by loom -------------------------------------------------
from .limits import (
    Classification,
    FailureKind,
    PAUSING_KINDS,
    classify_failure,
    pause_seconds,
)
from .test_integrity import IntegrityReport, scan_diff

# --- re-exported into loom's surface (still physically in devclaw for now) -----
from ..goal_models import (
    Action,
    EvalResult,
    Goal,
    GoalStatus,
    InFlight,
    PlanResult,
    PollResult,
)
from ..goal_store import GoalStore, parse_duration

__all__ = [
    # failure classification
    "classify_failure",
    "pause_seconds",
    "FailureKind",
    "Classification",
    "PAUSING_KINDS",
    # test-integrity guard
    "scan_diff",
    "IntegrityReport",
    # goal domain
    "Goal",
    "GoalStatus",
    "Action",
    "PlanResult",
    "EvalResult",
    "InFlight",
    "PollResult",
    # durable store
    "GoalStore",
    "parse_duration",
]
