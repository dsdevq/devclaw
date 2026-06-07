"""Auto-merge on gate-green — the hands-off half of the outcome-goals design
(decision 2: "after a unit's PR passes its gate, devclaw merges it itself and
pings a plain summary; the done-gate is the safety net").

Default OFF: merging to the default branch unsupervised is consequential, so it
is the owner's switch to flip (DEVCLAW_GOAL_AUTOMERGE=1). When on, the goal layer
squash-merges a delivered task's PR once its verify gate passed, and tells the
owner in plain language. Best-effort — a merge failure leaves the PR open for
manual review and never breaks the tick.

The gh call lives here (not in goal_tick) so the tick stays a pure, subprocess-free
unit under test; goal_service binds the real merger, tests inject a fake.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

#: takes a PR url, returns True iff it was merged.
Merger = Callable[[str], Awaitable[bool]]

AUTOMERGE_ENABLED = os.environ.get("DEVCLAW_GOAL_AUTOMERGE", "0") not in ("0", "false", "")
#: merge strategy flag for `gh pr merge`
_STRATEGY = "--" + (os.environ.get("DEVCLAW_GOAL_MERGE_STRATEGY", "squash") or "squash")


async def merge_pr(pr_url: str) -> bool:
    """Squash-merge a PR via gh. Best-effort: returns False on any failure (the
    caller leaves the PR open for manual review). Deletes the merged branch."""
    if not pr_url:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "merge", pr_url, _STRATEGY, "--delete-branch",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception:  # noqa: BLE001 — best-effort; never break the tick
        return False
    return proc.returncode == 0


def default_merger() -> Merger:
    """The production merger (real gh). Indirected so goal_service can bind it and
    tests inject a recording fake."""
    return merge_pr
