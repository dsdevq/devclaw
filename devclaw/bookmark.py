"""Per-workspace git bookmarks — last-seen-SHA persistence in the state store.

Used by the trend detector to track "what's new since last fire" without
re-iterating the whole git history every heartbeat. The detector's bookmark
lives in its OWN namespace (``trend_bookmark:<workspace>``) — separate from
any future engineer-brief bookmark so D1/D2 advancement can't interfere with
the engineer's repo-catch-up read (see plan.md "engineer brief" / PR3+).

Persistence shape mirrors the cooldown helpers landed in PR1: typed methods
on ``StateStore`` (``set_trend_bookmark`` / ``get_trend_bookmark``) backed by
the ``meta`` table. This module holds the small git-side utility the signals
need to ADVANCE that bookmark to HEAD.
"""

from __future__ import annotations

import subprocess
from typing import Optional

#: ``git rev-parse HEAD`` is fast; a 5-second cap is generous and still
#: catches a hung repo (network mount, fuse layer, etc.) without delaying the
#: heartbeat. Same shape as ``_run_git`` in ``trend_signals.py``.
_GIT_REVPARSE_TIMEOUT_SECONDS = 5


def git_head_sha(workspace_dir: str) -> Optional[str]:
    """Return the current HEAD SHA of ``workspace_dir``, or ``None`` on any
    failure (timeout, missing binary, non-git dir, detached state mid-rebase).
    Callers MUST tolerate ``None`` — treat it as "no bookmark possible" and
    skip the signal rather than crashing the heartbeat."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_REVPARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    # Defensive — short SHAs (some git configs) or empty output fail closed.
    return sha if len(sha) >= 40 else None
