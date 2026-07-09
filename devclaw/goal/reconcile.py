"""Reconcile a settled program's PR stack (mechanism, zero cognition).

A finished ``start_program`` leaves a stack of PRs, each based on the previous
(closeloop #66 → #67 → #68, 2026-07-08). Single-task auto-merge can't touch
them — a program settles with no single gate verdict — so historically the
goal burned follow-up dispatches shepherding the stack to main, and when one
of those dispatches landed the content as a consolidating squash (#70), the
source PRs were left open as zombies (live-found 2026-07-09: five open
closeloop PRs, all superseded, two CONFLICTING).

This walks the stack in task order at settle time and, per PR:

  * already merged/closed            → skip
  * content already on main          → close as superseded (reverse-apply test)
  * mergeable + required checks green → squash-merge via the same merger as
                                        single-task auto-merge
  * red CI / conflicting / unknown   → leave open and say so — dispatching a
                                        fix is the planner's call, not ours

Every branch of that decision is best-effort: any probe/close/merge failure
degrades to "left open" with the error in the summary. The returned summary
lines are appended to ``finished_detail`` so the planner plans its next action
against the stack's REAL state instead of inferring it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

Merger = Callable[[str], Awaitable[bool]]

#: check-run conclusions that don't block a merge
_CHECK_OK = {"SUCCESS", "NEUTRAL", "SKIPPED", ""}


async def _run(*argv: str, cwd: Optional[str] = None, stdin: Optional[bytes] = None) -> tuple[int, str]:
    """Run a command, capturing combined output. Never raises — a spawn
    failure is (1, message), consistent with 'leave the PR open' degradation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdin=(asyncio.subprocess.PIPE if stdin is not None else None),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(input=stdin)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the tick
        return 1, str(exc)
    return proc.returncode or 0, out.decode(errors="replace")


async def _pr_state(pr_url: str) -> dict:
    """PR facts from gh: state, mergeable, and whether checks are green.
    Returns {} on any failure (caller degrades to leave-open)."""
    code, out = await _run(
        "gh", "pr", "view", pr_url, "--json", "state,mergeable,statusCheckRollup",
    )
    if code != 0:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _checks_green(rollup: object) -> bool:
    """True when no required check is red or still running. An empty rollup
    (repo without CI) counts as green — same stance as `gh pr merge`."""
    if not rollup:
        return True
    if not isinstance(rollup, list):
        return False
    for check in rollup:
        conclusion = str((check or {}).get("conclusion") or "").upper()
        status = str((check or {}).get("status") or "").upper()
        if status and status != "COMPLETED":
            return False  # still running — don't merge under it
        if conclusion not in _CHECK_OK:
            return False
    return True


async def _superseded_by_main(pr_url: str, workspace_dir: str) -> bool:
    """Does the default branch already contain this PR's diff? Grounded test:
    reverse-apply the PR's diff onto a checkout of origin/<default> — success
    means every hunk is already present (the consolidating-squash case). Any
    step failing means 'unknown', reported as False (leave the PR open rather
    than close on a guess). The workspace is safe to move: every dispatch
    re-prepares it to a pristine checkout anyway."""
    code, out = await _run(
        "git", "-C", workspace_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
    )
    default = out.strip().removeprefix("origin/") if code == 0 and out.strip() else "main"
    code, _ = await _run("git", "-C", workspace_dir, "fetch", "origin", default)
    if code != 0:
        return False
    code, _ = await _run(
        "git", "-C", workspace_dir, "checkout", "-q", "--force", "--detach", f"origin/{default}",
    )
    if code != 0:
        return False
    code, diff = await _run("gh", "pr", "diff", pr_url)
    if code != 0 or not diff.strip():
        return False
    code, _ = await _run(
        "git", "-C", workspace_dir, "apply", "--reverse", "--check", "-",
        stdin=diff.encode(),
    )
    return code == 0


async def _close_superseded(pr_url: str) -> bool:
    code, _ = await _run(
        "gh", "pr", "close", pr_url, "--comment",
        "Superseded — this PR's changes are already on the default branch "
        "(devclaw reconcile-at-settle). Closing as housekeeping; nothing was lost.",
    )
    return code == 0


async def reconcile_stack(
    pr_urls: list[str], *, workspace_dir: str, merger: Merger,
    pr_state: Callable[[str], Awaitable[dict]] = _pr_state,
    superseded: Callable[[str, str], Awaitable[bool]] = _superseded_by_main,
    closer: Callable[[str], Awaitable[bool]] = _close_superseded,
) -> list[str]:
    """Walk the stack IN ORDER (base-most first — the engine reports program
    PRs in task order) and settle each PR. Returns one summary line per PR.
    Sequential on purpose: merging PR N re-bases N+1 and re-runs its checks,
    so N+1's state is only meaningful after N has landed."""
    summary: list[str] = []
    for url in pr_urls:
        state = await pr_state(url)
        if not state:
            summary.append(f"{url}: left open (state probe failed)")
            continue
        if str(state.get("state", "")).upper() != "OPEN":
            summary.append(f"{url}: already {str(state.get('state', 'settled')).lower()}")
            continue
        if await superseded(url, workspace_dir):
            closed = await closer(url)
            summary.append(
                f"{url}: closed (superseded by main)" if closed
                else f"{url}: superseded by main but close failed — left open"
            )
            continue
        mergeable = str(state.get("mergeable", "")).upper()
        if mergeable == "CONFLICTING":
            summary.append(f"{url}: left open (conflicts with main — needs a fix dispatch)")
            continue
        if not _checks_green(state.get("statusCheckRollup")):
            summary.append(f"{url}: left open (checks red or pending — needs a fix dispatch or a later retry)")
            continue
        if await merger(url):
            summary.append(f"{url}: merged")
        else:
            summary.append(f"{url}: left open (merge attempt failed)")
    return summary
