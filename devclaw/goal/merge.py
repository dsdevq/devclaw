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

Configuration lives in exactly two places, deliberately NOT in goal.yaml: the
devclaw-wide default (``DEVCLAW_GOAL_AUTOMERGE``, this module) and an optional
per-project override (``Project.automerge`` in :mod:`devclaw.project_registry`,
resolved by :func:`resolve_automerge`). A goal itself has no automerge field —
merging is an ops/deploy-scope decision the owner makes about a REPO, not
something a goal's own objective should carry (found 2026-07-05: a stray
``automerge: true`` hand-written into a goal.yaml did nothing at all, silently,
because nothing ever read it — the only real switch was the global env var).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from ..project_registry import ProjectRegistry

#: takes a PR url, returns True iff it was merged.
Merger = Callable[[str], Awaitable[bool]]

#: the devclaw-wide default when a project has no override of its own.
AUTOMERGE_ENABLED = False
#: the devclaw-wide default merge strategy — a project may override it.
_VALID_STRATEGIES = ("squash", "merge", "rebase")
DEFAULT_MERGE_STRATEGY = "squash"


def resolve_automerge(
    registry: "Optional[ProjectRegistry]", workspace_dir: Optional[str]
) -> bool:
    """Should a goal working in ``workspace_dir`` auto-merge its gate-passed
    PRs? A project's own ``automerge`` override wins when set; otherwise this
    falls back to the devclaw-wide ``AUTOMERGE_ENABLED`` default. With no
    registry (e.g. tests, or a workspace not registered as a project), the
    global default is all there is."""
    if registry is not None:
        project = registry.find_by_workspace_dir(workspace_dir)
        if project is not None and project.automerge is not None:
            return project.automerge
    return AUTOMERGE_ENABLED


def resolve_merge_strategy(
    registry: "Optional[ProjectRegistry]", workspace_dir: Optional[str]
) -> str:
    """Which `gh pr merge` strategy for a goal working in ``workspace_dir``: the
    owning project's ``merge_strategy`` override if set, else the devclaw-wide
    ``DEFAULT_MERGE_STRATEGY``. A pinned-but-invalid value falls back to the
    default rather than handing `gh` a bad flag."""
    strategy = DEFAULT_MERGE_STRATEGY
    if registry is not None:
        strategy = registry.resolve_override(workspace_dir, "merge_strategy", DEFAULT_MERGE_STRATEGY)
    return strategy if strategy in _VALID_STRATEGIES else DEFAULT_MERGE_STRATEGY


async def merge_pr(pr_url: str, strategy: str = DEFAULT_MERGE_STRATEGY) -> bool:
    """Merge a PR via gh with the given strategy (default from
    ``DEVCLAW_GOAL_MERGE_STRATEGY``). Best-effort: returns False on any failure
    (the caller leaves the PR open for manual review). Deletes the merged
    branch."""
    if not pr_url:
        return False
    flag = "--" + (strategy if strategy in _VALID_STRATEGIES else DEFAULT_MERGE_STRATEGY)
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "merge", pr_url, flag, "--delete-branch",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception:  # noqa: BLE001 — best-effort; never break the tick
        return False
    return proc.returncode == 0


def default_merger(strategy: str = DEFAULT_MERGE_STRATEGY) -> Merger:
    """The production merger (real gh), bound to ``strategy`` so goal_service
    can pass a project's resolved merge strategy. Indirected so tests inject a
    recording fake."""
    async def _merge(pr_url: str) -> bool:
        return await merge_pr(pr_url, strategy)
    return _merge
