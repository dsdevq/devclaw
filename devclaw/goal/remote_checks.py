"""Grounded remote-checks verification — "is the target repo's REAL CI green?".

The 2026-07-06 quarterly benchmark (closeloop-bench-2026-07-05) closed a goal
whose GitHub Actions had failed at startup on all 32 runs: the sandbox verify
gate passed, the internal log said "gate=passed", and nothing in the chain ever
looked at the repo's actual check surface. Silence read as success.

This module is the look. At the done-gate, when the goal's work lives on a
shared goal branch with a PR, devclaw queries the REAL check state for that
branch's head commit — both the check-runs API (any CI provider) and the
Actions runs API (which is where `startup_failure` runs appear even when the
check-runs list is empty, exactly the benchmark's failure signature) — and an
``achieved`` verdict is only honored when the remote checks don't contradict it.

Verdict semantics (deliberately fail-open on *infrastructure* uncertainty,
fail-closed on *evidence* of a problem):

- ``passing``      → close as normal.
- ``failing``      → block the close; the corrections steer a fix.
- ``pending``      → block the close; re-propose once the runs settle.
- ``none``         → workflows exist but produced zero runs/checks for this
                     commit — the benchmark's "CI never ran and nobody noticed"
                     case. Blocks the close.
- ``no_workflows`` → the repo has no ``.github/workflows`` — nothing to check;
                     does not block (authoring CI is the checklist's job, per
                     plan.md §Production-ready C5, not the gate's).
- ``unknown``      → gh/network error or a non-GitHub remote — logged loudly,
                     does not block (an infra flake must not wedge a verified
                     goal; the log line keeps it observable).

The gh calls live here (not in goal_tick) so the tick stays a pure,
subprocess-free unit under test; goal_service binds the real checker, tests
inject a fake — the same seam shape as ``merge.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

#: takes (repo_url, branch), returns the combined remote-check verdict.
RemoteChecker = Callable[[str, str], Awaitable["RemoteChecksResult"]]

REMOTE_CHECKS_ENABLED = os.environ.get("DEVCLAW_GOAL_REMOTE_CHECKS", "1") not in ("0", "false", "")

#: conclusions that contradict "this work is done". ``cancelled`` counts: a
#: run that never finished proved nothing, and we only query THIS commit's
#: runs so stale cancels don't bleed in.
_BAD_CONCLUSIONS = {"failure", "startup_failure", "timed_out", "action_required", "cancelled"}
_PENDING_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}

_OWNER_REPO_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class RemoteChecksResult:
    """The combined state of a branch head's real checks."""

    state: str  # passing | failing | pending | none | no_workflows | unknown
    detail: str = ""

    @property
    def blocks_done(self) -> bool:
        return self.state in ("failing", "pending", "none")


def parse_owner_repo(repo_url: str) -> Optional[str]:
    """``https://github.com/o/r.git`` / ``git@github.com:o/r.git`` → ``o/r``.
    None for anything that isn't GitHub — the checker then reports ``unknown``
    (fail-open) rather than guessing at a forge it can't query."""
    if not repo_url:
        return None
    m = _OWNER_REPO_RE.search(repo_url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def combine_states(
    runs: Optional[list[dict]],
    checks: Optional[list[dict]],
    *,
    workflows_present: bool,
) -> RemoteChecksResult:
    """Fold Actions runs + check-runs for one commit into a single verdict.
    Pure function — the subprocess boundary stays in :func:`check_branch`.

    ``runs`` / ``checks`` are lists of ``{"status": ..., "conclusion": ...}``;
    ``None`` means that API call itself failed (distinct from an empty list).
    """
    if runs is None and checks is None:
        return RemoteChecksResult("unknown", "both check-runs and Actions-runs queries failed")
    items = (runs or []) + (checks or [])
    if not items:
        if not workflows_present:
            return RemoteChecksResult("no_workflows", "no .github/workflows in the repo — nothing to check")
        return RemoteChecksResult(
            "none",
            "workflows exist but produced zero runs/check-runs for this commit — "
            "CI never ran (check Actions permissions/billing on the repo)",
        )
    conclusions: dict[str, int] = {}
    pending = 0
    for it in items:
        status = str(it.get("status") or "").lower()
        conclusion = str(it.get("conclusion") or "").lower()
        if status in _PENDING_STATUSES or (status != "completed" and not conclusion):
            pending += 1
            continue
        conclusions[conclusion] = conclusions.get(conclusion, 0) + 1
    summary = ", ".join(f"{n}× {c or '(none)'}" for c, n in sorted(conclusions.items())) or "no settled runs"
    bad = sum(n for c, n in conclusions.items() if c in _BAD_CONCLUSIONS)
    if bad:
        return RemoteChecksResult("failing", f"{bad} failed of {len(items)} ({summary})")
    if pending:
        return RemoteChecksResult("pending", f"{pending} of {len(items)} still running ({summary})")
    return RemoteChecksResult("passing", f"{len(items)} checks settled ({summary})")


async def _gh(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — the caller maps this to "unknown"
        return 1, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode or 0, out.decode(errors="replace")


def _parse_json_list(rc: int, out: str) -> Optional[list[dict]]:
    if rc != 0:
        return None
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


async def check_branch(repo_url: str, branch: str) -> RemoteChecksResult:
    """The real checker: resolve the branch head on GitHub, gather its Actions
    runs + check-runs, and fold them. Best-effort throughout — every failure
    path degrades to ``unknown`` rather than raising into the tick."""
    owner_repo = parse_owner_repo(repo_url)
    if not owner_repo:
        return RemoteChecksResult("unknown", f"not a GitHub remote: {repo_url!r}")

    rc, out = await _gh("api", f"repos/{owner_repo}/commits/{branch}", "--jq", ".sha")
    if rc != 0:
        return RemoteChecksResult("unknown", f"could not resolve {branch!r} head: {out.strip()[:200]}")
    sha = out.strip()

    rc_wf, out_wf = await _gh(
        "api", f"repos/{owner_repo}/contents/.github/workflows?ref={branch}", "--jq", "length",
    )
    workflows_present = rc_wf == 0 and out_wf.strip().isdigit() and int(out_wf.strip()) > 0

    rc_r, out_r = await _gh(
        "api", f"repos/{owner_repo}/actions/runs?head_sha={sha}&per_page=50",
        "--jq", "[.workflow_runs[] | {status, conclusion}]",
    )
    runs = _parse_json_list(rc_r, out_r)

    rc_c, out_c = await _gh(
        "api", f"repos/{owner_repo}/commits/{sha}/check-runs",
        "--jq", "[.check_runs[] | {status, conclusion}]",
    )
    checks = _parse_json_list(rc_c, out_c)

    return combine_states(runs, checks, workflows_present=workflows_present)


def default_checker() -> RemoteChecker:
    """The production checker (real gh). Indirected so goal_service can bind it
    and tests inject a recording fake — the merge.py seam shape."""
    return check_branch
