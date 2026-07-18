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
import re
import sys
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
#: The commit-status context devclaw posts when its own gates pass. A repo can
#: require it in branch protection so GitHub-native auto-merge (``--auto``) has a
#: required check to gate on — devclaw becomes the authoritative CI, no GitHub
#: Actions needed. devclaw only reaches merge AFTER verify+integrity+review+browser
#: gates passed, so a ``success`` here is truthful.
GATE_STATUS_CONTEXT = "devclaw/gate"


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


async def _run_gh(*argv: str) -> tuple[int, str]:
    """Run a subprocess, returning ``(returncode, combined stdout+stderr)``.
    Best-effort: a spawn failure returns ``(-1, "<Exc>: msg")`` and never raises
    into the tick. Shared by the SHA fetch, the status post, and the merge call."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the tick
        return -1, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, out.decode(errors="replace").strip()


def _owner_repo(pr_url: str) -> Optional[str]:
    """``owner/repo`` from a GitHub PR url, else None."""
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)/pull/\d+", pr_url)
    return m.group(1) if m else None


async def _post_gate_status(pr_url: str) -> None:
    """Post ``GATE_STATUS_CONTEXT=success`` on the PR's head commit so a
    branch-protected repo's GitHub-native auto-merge has its required check
    satisfied. Best-effort + never raises: harmless where the check isn't
    required (just an extra green check), and a failure only means ``--auto``
    falls back to a direct merge below. Truthful — devclaw reaches here only
    after its own gates passed."""
    repo = _owner_repo(pr_url)
    if not repo:
        return
    rc, sha = await _run_gh("gh", "pr", "view", pr_url, "--json", "headRefOid",
                            "-q", ".headRefOid")
    if rc != 0 or not sha:
        return
    await _run_gh(
        "gh", "api", "--method", "POST", f"repos/{repo}/statuses/{sha}",
        "-f", "state=success", "-f", f"context={GATE_STATUS_CONTEXT}",
        "-f", "description=devclaw gates passed (verify+integrity+review+browser)",
    )


async def merge_pr(pr_url: str, strategy: str = DEFAULT_MERGE_STRATEGY) -> bool:
    """Merge a PR via gh with the given strategy (default from
    ``DEVCLAW_GOAL_MERGE_STRATEGY``). Best-effort: returns False on any failure
    (the caller leaves the PR open for manual review). Deletes the merged branch.

    Prefers **GitHub-native auto-merge** (``gh pr merge --auto``): GitHub resolves
    mergeability and waits on the required check SERVER-SIDE, then merges. This
    kills the client-side mergeability race — a fresh PR reports ``mergeable:
    UNKNOWN`` for seconds after creation, and an immediate blind ``gh pr merge``
    fails on that, so the goal wrongly blocked "please merge" and stalled for
    hours (finance-sentry, 2026-07-17). With ``--auto`` a True return means the
    merge is *enabled/queued* (GitHub completes it) — the caller's tip re-check
    before the next task tolerates the eventual-merge delay.

    ORDER MATTERS (2026-07-18 finance-sentry diagnosis): ``--auto`` is enabled
    FIRST, while the PR is still blocked on the not-yet-posted required check —
    GitHub only accepts enabling auto-merge on a PR that has something left to
    wait for. Posting the gate status first can make the PR "clean", ``--auto``
    then errors ("clean status") and the merge degrades to the client-side
    direct attempt, which fires sub-second after PR creation while
    ``mergeStateStatus`` is still UNKNOWN and loses that race essentially
    always (every recent finance-sentry PR ended up owner-merged by hand).
    Enable-then-post lets GitHub resolve everything server-side — no client
    polling, no race.

    Falls back to a direct merge for repos where GitHub auto-merge isn't enabled
    (``--auto`` errors "clean status"/"not allowed") — byte-identical to the
    pre-change behavior there, so nothing regresses without the repo config."""
    if not pr_url:
        return False
    flag = "--" + (strategy if strategy in _VALID_STRATEGIES else DEFAULT_MERGE_STRATEGY)
    # Enable server-side auto-merge FIRST (see ORDER MATTERS above)…
    rc, out = await _run_gh("gh", "pr", "merge", pr_url, "--auto", flag, "--delete-branch")
    # …then post devclaw's gate as the required check: with --auto enabled it is
    # the green light GitHub merges on; on the fallback path it is a truthful,
    # harmless extra check. Best-effort either way.
    await _post_gate_status(pr_url)
    if rc == 0:
        return True
    # --auto declined (repo has no auto-merge enabled) → direct merge, as before.
    sys.stderr.write(
        f"merge.merge_pr: {pr_url} --auto declined (rc={rc}: {out[:200] or 'no output'}) "
        f"— falling back to a direct {flag} merge\n"
    )
    rc2, out2 = await _run_gh("gh", "pr", "merge", pr_url, flag, "--delete-branch")
    if rc2 != 0:
        # Surface WHY — the caller only sees the bool and pings the owner, but the
        # operator debugging "automerge isn't firing" needs the gh reason.
        sys.stderr.write(
            f"merge.merge_pr: {pr_url} not merged (rc={rc2}): {out2[:300] or 'no output'}\n"
        )
        return False
    return True


def default_merger(strategy: str = DEFAULT_MERGE_STRATEGY) -> Merger:
    """The production merger (real gh), bound to ``strategy`` so goal_service
    can pass a project's resolved merge strategy. Indirected so tests inject a
    recording fake."""
    async def _merge(pr_url: str) -> bool:
        return await merge_pr(pr_url, strategy)
    return _merge
