"""Trend detection signals — cross-session pattern detectors.

The mechanism half of the trend-detection discipline (designed 2026-06-29). Each
signal is a deterministic Python check that runs every heartbeat and may fire a
trend observation. Signals are intentionally cheap and read-only: the cognition
half (the LLM retrospective pass that writes the trends.md entry) is gated
behind a fired signal, so quiet projects cost zero tokens.

This module is the place to add a new signal. Each signal is a subclass of
:class:`Signal` with a class-level ``id`` / ``category`` / ``scope`` and a
single ``check(ctx)`` method that returns a :class:`SignalResult`. Substrate
reads live INLINE in the signal — no separate substrate adapters until enough
signals share SQL to justify the dedup.

Boundary rules (carried from the thesis — do not violate inside ``check``):
  * NEVER call any goal-store write method (no ``append_steering``,
    ``write_checklist``, etc.). The detector observes; humans encode.
  * NEVER create tasks, alter ``done_when``, or edit ``AGENTS.md``.
  * Substrate reads only — git plumbing, ``GoalStore._inbox_lines`` (read), the
    sqlite ``tasks`` / ``traces`` tables (read). If a future signal looks like
    it needs a write, that's a design-bug — escalate, don't smuggle it in.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

Category = Literal["recurrence", "drift", "harness_self", "goal_direction"]
Scope = Literal["per_project", "harness_self"]

# ---- module-level helpers (test-seamable: patch _run_git for git signals;
# `_parse_inbox_denys_lines` is a pure function on file content) -------------

_GIT_TIMEOUT_SECONDS = 10

#: Fix-class commits: the message contains a whole-word match of one of these.
#: Matched case-insensitively against the commit subject + body via `git log
#: --grep -E -i`. Conservative on purpose (no "patch" / "tweak" — too noisy).
_FIX_GREP_PATTERN = r"\b(fix|bug|regression|hotfix)\b"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Matches a `denys`-sourced inbox steering line produced by
#: ``GoalStore.append_steering(... source='denys')`` — one line per steering,
#: prefix is ``- [denys <iso-ts>] `` (see ``goal/store.py:381``).
_INBOX_DENYS_LINE = re.compile(r"^-\s*\[denys\s+([0-9T\-:+.Z]+)\]\s+(.*)$")


def _run_git(args: list[str], cwd: str) -> str:
    """Run ``git <args>`` in ``cwd`` and return stdout, or ``""`` on any
    failure (timeout, missing binary, non-git dir, non-zero exit). Signals are
    intentionally defensive — a broken git invocation must never raise out of
    the heartbeat."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _parse_git_log_name_only(out: str) -> list[tuple[str, list[str]]]:
    """Parse ``git log --pretty=format:%H --name-only`` output into
    ``[(sha, [paths]), ...]`` in newest-first order. A SHA line is exactly 40
    hex chars; file paths follow until the next blank line or next SHA."""
    entries: list[tuple[str, list[str]]] = []
    current_sha: Optional[str] = None
    current_files: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            if current_sha is not None:
                entries.append((current_sha, current_files))
                current_sha, current_files = None, []
            continue
        if _SHA_RE.match(s):
            if current_sha is not None:
                entries.append((current_sha, current_files))
            current_sha, current_files = s, []
        else:
            if current_sha is not None:
                current_files.append(s)
    if current_sha is not None:
        entries.append((current_sha, current_files))
    return entries


def _parse_inbox_denys_lines(path: Path) -> list[tuple[int, str]]:
    """Read an inbox.md and return ``[(ts_ms, text), ...]`` for lines whose
    source is ``denys``. ``auto-eval`` and other-source lines are skipped on
    purpose — H4 measures human-correction frequency, not total steering."""
    if not path.exists():
        return []
    try:
        content = path.read_text()
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    for line in content.splitlines():
        m = _INBOX_DENYS_LINE.match(line)
        if not m:
            continue
        ts_raw, text = m.group(1), m.group(2)
        ts_clean = ts_raw.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_clean)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append((int(ts.timestamp() * 1000), text))
    return out


@dataclass
class SignalContext:
    """The narrow handle each ``Signal.check`` receives. Intentionally minimal
    so the boundary is structural: a signal cannot reach anything not in here."""

    scope: Scope
    #: Workspace repo on disk (per-project signals). ``None`` for harness-self.
    workspace_dir: Optional[str]
    #: The goal currently being ticked (per-project signals). ``None`` for
    #: harness-self signals which run once per heartbeat across all goals.
    goal_id: Optional[str]
    #: Root of the on-disk goal store (``~/memory/goals`` or
    #: ``DEVCLAW_GOALS_DIR``). Used by harness-self signals that traverse goals.
    goals_dir: Path
    #: Current time in epoch milliseconds. Injectable so tests are deterministic.
    now_ms: int


@dataclass
class SignalResult:
    """What ``Signal.check`` returns. ``fired=False`` is the common case (most
    pre-filter passes don't fire). When ``fired=True``, ``evidence`` is the
    bounded payload the LLM retrospective pass receives; ``deeper_refs`` lists
    paths or commands the LLM can read on demand if it wants more context."""

    fired: bool
    actual_value: Optional[float] = None
    threshold_value: Optional[float] = None
    evidence: dict = field(default_factory=dict)
    deeper_refs: dict = field(default_factory=dict)


class Signal:
    """Base class for a pre-filter check.

    Subclasses override ``check``. The class-level attributes ``id`` /
    ``category`` / ``scope`` / ``cooldown_hours`` are required (mypy-friendly
    when subclasses set them explicitly). ``check`` must be cheap, read-only,
    and total — exceptions are caught by the orchestrator and recorded as a
    no-fire with ``reason='error: …'``, but raising is still a smell."""

    id: str = ""
    category: Category = "drift"
    scope: Scope = "per_project"
    cooldown_hours: int = 24

    def check(self, ctx: SignalContext) -> SignalResult:
        raise NotImplementedError


# ---- v1 signals (Stage 2 fills in the check() bodies) ----------------------


class R2RepeatedFixHotspot(Signal):
    """Same file in ≥4 fix-class commits in the trailing 30-day window.

    Reads ``git log -E -i --grep='\\b(fix|bug|regression|hotfix)\\b'
    --name-only`` over the workspace repo and tallies per-file occurrences.
    Fires when any file crosses the threshold — signal-to-noise is highest on
    repos that use conventional-commit ``fix(...)`` prefixes; lower on
    free-form repos. Empirical calibration after PR1."""

    id = "R2"
    category = "recurrence"
    scope = "per_project"

    #: Trailing window for the fix-class commit search.
    window_days = 30
    #: Fire when the same file appears in ≥N fix-class commits within the window.
    hotspot_threshold = 4

    def check(self, ctx: SignalContext) -> SignalResult:
        if not ctx.workspace_dir:
            return SignalResult(fired=False)
        out = _run_git(
            [
                "log",
                "--extended-regexp",
                "-i",
                f"--grep={_FIX_GREP_PATTERN}",
                "--name-only",
                "--pretty=format:%H",
                f"--since={self.window_days} days ago",
                "--no-merges",
            ],
            cwd=ctx.workspace_dir,
        )
        entries = _parse_git_log_name_only(out)
        per_file: dict[str, list[str]] = {}
        for sha, files in entries:
            for f in files:
                per_file.setdefault(f, []).append(sha)
        worst_file: Optional[str] = None
        worst_count = 0
        for f, shas in per_file.items():
            if len(shas) > worst_count:
                worst_file = f
                worst_count = len(shas)
        if worst_file is None or worst_count < self.hotspot_threshold:
            return SignalResult(
                fired=False,
                actual_value=float(worst_count),
                threshold_value=float(self.hotspot_threshold),
            )
        return SignalResult(
            fired=True,
            actual_value=float(worst_count),
            threshold_value=float(self.hotspot_threshold),
            evidence={
                "file": worst_file,
                "fix_class_commit_count": worst_count,
                "commit_shas": per_file[worst_file][:8],
                "window_days": self.window_days,
            },
            deeper_refs={
                "git_log_cmd": (
                    f"git log -E -i --grep='{_FIX_GREP_PATTERN}' --name-only "
                    f"--pretty=format:%H --since='{self.window_days} days ago' "
                    f"-- {worst_file}"
                ),
            },
        )


class D4AgentsMdStaleness(Signal):
    """``AGENTS.md`` untouched while the project has had material churn.

    Fires when ``AGENTS.md`` has not been committed for ≥30 commits AND the
    project itself has had ≥30 total commits. A strict subset of PR2's D2
    (files-touched-not-in-AGENTS.md) — once D2 lands, plan to retire D4."""

    id = "D4"
    category = "drift"
    scope = "per_project"

    #: Threshold for both "commits since AGENTS.md touched" AND "total project commits".
    commits_threshold = 30

    def check(self, ctx: SignalContext) -> SignalResult:
        if not ctx.workspace_dir:
            return SignalResult(fired=False)
        all_commits_raw = _run_git(
            ["log", "--pretty=format:%H", "--no-merges"],
            cwd=ctx.workspace_dir,
        )
        all_commits = [s for s in all_commits_raw.splitlines() if s.strip()]
        if len(all_commits) < self.commits_threshold:
            # Not enough history for the signal to mean anything yet.
            return SignalResult(
                fired=False,
                actual_value=float(len(all_commits)),
                threshold_value=float(self.commits_threshold),
            )
        agents_commits_raw = _run_git(
            ["log", "--pretty=format:%H", "--no-merges", "--", "AGENTS.md"],
            cwd=ctx.workspace_dir,
        )
        agents_commits = [s for s in agents_commits_raw.splitlines() if s.strip()]
        if not agents_commits:
            # AGENTS.md never committed. Project with ≥30 commits and no AGENTS.md
            # at all is itself the drift signal — every commit is "since".
            commits_since = len(all_commits)
        else:
            last_agents_sha = agents_commits[0]
            try:
                commits_since = all_commits.index(last_agents_sha)
            except ValueError:
                # AGENTS.md was touched on a commit not in the merge-free
                # log (e.g. only on a merge). Defensive: treat as untouched.
                commits_since = len(all_commits)
        if commits_since < self.commits_threshold:
            return SignalResult(
                fired=False,
                actual_value=float(commits_since),
                threshold_value=float(self.commits_threshold),
            )
        return SignalResult(
            fired=True,
            actual_value=float(commits_since),
            threshold_value=float(self.commits_threshold),
            evidence={
                "commits_since_agents_md_touched": commits_since,
                "total_commits": len(all_commits),
                "last_agents_md_sha": agents_commits[0] if agents_commits else None,
                "agents_md_exists_in_history": bool(agents_commits),
            },
            deeper_refs={
                "git_log_cmd": "git log --pretty=format:%H --no-merges -- AGENTS.md",
            },
        )


class H4SteeringFrequency(Signal):
    """Active goals receiving ≥3 ``denys``-sourced steerings grew vs the prior
    period.

    Reads each goal's ``inbox.md`` via the same parsed-prefix shape
    ``GoalStore._inbox_lines`` uses (lines of the form
    ``- [denys 2026-…] correction text``), filters to ``source='denys'``, and
    counts per goal in two windows of equal width (current vs prior). Fires
    when the count of "actively-steered" goals (≥3 denys lines in window) is
    strictly greater in the current window than the prior window.

    Min-history guard: returns ``fired=False`` when the goal store contains
    ``< 5 goal directories`` OR its oldest ``goal.yaml`` is ``< 14 days`` old.
    Without the guard, fresh installs would fire spuriously in week 1."""

    id = "H4"
    category = "harness_self"
    scope = "harness_self"
    cooldown_hours = 24

    min_history_days = 14
    min_history_goals = 5

    #: Width of the current AND prior comparison windows (each).
    window_days = 7
    #: A goal with ≥N denys lines in a window is "actively being steered".
    steerings_per_goal_threshold = 3

    def check(self, ctx: SignalContext) -> SignalResult:
        goals_dir = ctx.goals_dir
        if not goals_dir.exists():
            return SignalResult(fired=False)

        goal_dirs = [
            d for d in goals_dir.iterdir()
            if d.is_dir() and (d / "goal.yaml").is_file()
        ]
        if len(goal_dirs) < self.min_history_goals:
            return SignalResult(
                fired=False,
                actual_value=float(len(goal_dirs)),
                threshold_value=float(self.min_history_goals),
            )

        now_ms = ctx.now_ms
        try:
            oldest_mtime_ms = int(min(
                (d / "goal.yaml").stat().st_mtime * 1000 for d in goal_dirs
            ))
        except (OSError, ValueError):
            return SignalResult(fired=False)
        history_age_days = (now_ms - oldest_mtime_ms) / (1000.0 * 86400.0)
        if history_age_days < self.min_history_days:
            return SignalResult(
                fired=False,
                actual_value=history_age_days,
                threshold_value=float(self.min_history_days),
            )

        window_ms = self.window_days * 86400 * 1000
        current_cutoff = now_ms - window_ms
        prior_cutoff = now_ms - 2 * window_ms

        current_counts: dict[str, int] = {}
        prior_counts: dict[str, int] = {}
        for d in goal_dirs:
            for ts_ms, _text in _parse_inbox_denys_lines(d / "inbox.md"):
                if ts_ms >= current_cutoff:
                    current_counts[d.name] = current_counts.get(d.name, 0) + 1
                elif ts_ms >= prior_cutoff:
                    prior_counts[d.name] = prior_counts.get(d.name, 0) + 1

        current_active = sum(
            1 for n in current_counts.values() if n >= self.steerings_per_goal_threshold
        )
        prior_active = sum(
            1 for n in prior_counts.values() if n >= self.steerings_per_goal_threshold
        )

        if current_active <= prior_active:
            return SignalResult(
                fired=False,
                actual_value=float(current_active),
                threshold_value=float(prior_active + 1),
            )

        return SignalResult(
            fired=True,
            actual_value=float(current_active),
            threshold_value=float(prior_active + 1),
            evidence={
                "window_days": self.window_days,
                "goals_with_3plus_denys_steerings_now": current_active,
                "goals_with_3plus_denys_steerings_prior": prior_active,
                "current_steering_counts_top5": dict(
                    sorted(current_counts.items(), key=lambda kv: -kv[1])[:5]
                ),
            },
            deeper_refs={"goals_dir": str(goals_dir)},
        )


# ---- registry --------------------------------------------------------------


def all_signals() -> list[Signal]:
    """The v1 signal set. PR2/3/4/5 extend this. Order doesn't determine fire
    priority (the orchestrator owns that — see ``trend_detector._SIGNAL_PRIORITY``)."""
    return [
        R2RepeatedFixHotspot(),
        D4AgentsMdStaleness(),
        H4SteeringFrequency(),
    ]
