"""loom — the reusable orchestration core, sans the ``-claw`` prefix.

This package is the **extraction seam**: the engine-agnostic substrate devclaw is
built on, grouped under a neutral name so it can eventually become a standalone,
reusable library (an orchestrator weaving many threads — goals, tasks, agents —
into delivered work). devclaw remains the concrete product (the MCP server, the
OpenHands engine, the GitHub delivery); loom is the part with no opinion about
*which* engine or product uses it.

What lives here (physically, all of it pure stdlib): :mod:`~devclaw.loom.limits`
(the usage-limit/rate-limit failure classifier), :mod:`~devclaw.loom.test_integrity`
(the gate's deleted/weakened-test guard), and :mod:`~devclaw.loom.trace` (the
run-trace capture). Old import paths (``devclaw.limits`` etc.) keep working via
thin shims, so this extraction is reversible and non-breaking.

loom is a LEAF by contract (pinned by ``tests/test_llm_call_leaf.py``): it
imports nothing from the rest of devclaw. The goal domain types + store used to
be re-exported here as a "curated surface", which made importing ``loom.trace``
execute this facade and drag ``goal`` + ``state_store`` behind every consumer —
the exact cycle the extraction seam exists to prevent. Import those from
``devclaw.goal`` directly; import the core from one place::

    from devclaw.loom import classify_failure, scan_diff
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
]
