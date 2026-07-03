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
    #: Trend-detector bookmark for this workspace — the last-seen SHA the
    #: detector observed here. Populated by the orchestrator from the meta
    #: table (seeded to current HEAD on first observation). Bookmark-aware
    #: signals (D1, D2, D3) read this; non-bookmark signals (R2, D4, H4) ignore
    #: it. ``None`` only when not applicable (harness-self scope, or git failed).
    bookmark: Optional[str] = None


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
    no-fire with ``reason='error: …'``, but raising is still a smell.

    ``advances_bookmark`` marks signals that READ ``ctx.bookmark`` and whose
    fires represent an "observation event" worth resetting the window. The
    orchestrator advances the trend bookmark to HEAD after persistence ONLY
    when the firing signal has this set. Non-bookmark-aware signals (R2, D4,
    H4) leave it ``False`` so their fires don't reset other signals' windows."""

    id: str = ""
    category: Category = "drift"
    scope: Scope = "per_project"
    cooldown_hours: int = 24
    advances_bookmark: bool = False

    def check(self, ctx: SignalContext) -> SignalResult:
        raise NotImplementedError

    def fingerprint(self, result: "SignalResult") -> str:
        """Return a stable identity of the situation this fire is describing.

        Two fires with the same fingerprint mean "same underlying evidence,
        same story" — the trend detector uses this to suppress duplicate
        retrospective LLM passes (and duplicate owner notifications) after
        the initial fire, per the 2026-07-03 audit that surfaced R2 firing
        4 days in a row on an unchanged 7-commit set.

        Default: a stable hash over every key/value in ``result.evidence``.
        Signals whose evidence contains volatile fields (e.g. a monotonically
        growing counter of commits since a doc was touched) MUST override
        this to hash only the stability-defining subset — otherwise the
        fingerprint drifts every commit and mute is defeated.

        The mute is layered on top of the time cooldown (not a replacement):
        cooldown enforces "don't check again for N hours", fingerprint
        enforces "don't fire again on the same story ever, until it changes."
        """
        import hashlib
        import json

        stable = json.dumps(result.evidence or {}, sort_keys=True, default=str)
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


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
            # deeper_refs set below (unchanged).
            deeper_refs={
                "git_log_cmd": (
                    f"git log -E -i --grep='{_FIX_GREP_PATTERN}' --name-only "
                    f"--pretty=format:%H --since='{self.window_days} days ago' "
                    f"-- {worst_file}"
                ),
            },
        )

    def fingerprint(self, result: "SignalResult") -> str:
        """R2's identity is the (file, sorted-SHA-set) tuple: same file with
        the same set of fix-class commits = same story. New fix commits on
        the same file → different fingerprint → refire (real new evidence).
        Fixes on other files → different file → different fingerprint.

        The window is trailing-30-days so as time passes old commits fall
        off — but the fingerprint hashes the CURRENT commit set (sorted so
        order doesn't matter), so an unchanged 30-day window over multiple
        days produces the same fingerprint even as the calendar advances."""
        import hashlib

        ev = result.evidence or {}
        file = ev.get("file", "")
        shas = tuple(sorted(ev.get("commit_shas", []) or []))
        stable = f"r2-hotspot:file={file}:shas={','.join(shas)}"
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


class _DocFileStaleness(Signal):
    """Base for "single documentation file untouched while the project has had
    material churn" signals. Subclasses pin ``id``, ``doc_path``, and (rarely)
    ``commits_threshold``.

    Fires when the tracked doc has not been committed for ≥``commits_threshold``
    commits AND the project itself has had ≥``commits_threshold`` total commits.
    All doc-drift signals share this shape verbatim — parameterizing avoids
    four near-duplicate 60-line ``check`` methods. Category and scope are set
    on the concrete subclass so the trend detector's priority + dedup treat
    each doc independently."""

    category = "drift"
    scope = "per_project"

    #: Concrete subclasses override to point at the file this signal tracks.
    doc_path: str = ""
    #: Threshold for both "commits since doc touched" AND "total project commits".
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
        doc_commits_raw = _run_git(
            ["log", "--pretty=format:%H", "--no-merges", "--", self.doc_path],
            cwd=ctx.workspace_dir,
        )
        doc_commits = [s for s in doc_commits_raw.splitlines() if s.strip()]
        if not doc_commits:
            # The doc has never been committed. A project with ≥threshold
            # commits and no doc at all IS the drift signal — every commit
            # counts as "since."
            commits_since = len(all_commits)
        else:
            last_doc_sha = doc_commits[0]
            try:
                commits_since = all_commits.index(last_doc_sha)
            except ValueError:
                # Doc was touched only on a merge commit, so it isn't in the
                # merge-free log. Defensive: treat as untouched.
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
                f"commits_since_{self._evidence_slug()}_touched": commits_since,
                "total_commits": len(all_commits),
                f"last_{self._evidence_slug()}_sha": doc_commits[0] if doc_commits else None,
                f"{self._evidence_slug()}_exists_in_history": bool(doc_commits),
            },
            deeper_refs={
                "git_log_cmd": (
                    f"git log --pretty=format:%H --no-merges -- {self.doc_path}"
                ),
            },
        )

    def _evidence_slug(self) -> str:
        """Stable slug (``AGENTS.md`` → ``agents_md``) used in the evidence
        keys so each doc-signal's dict is inspectable independently."""
        return self.doc_path.lower().replace(".", "_").replace("/", "_")

    def fingerprint(self, result: "SignalResult") -> str:
        """Doc-staleness identity is ``last_<doc>_sha`` — the SHA of the
        commit that last touched the doc (or None if never). The commits-since
        counter grows every commit and would defeat the mute; the "story"
        this signal is telling is "doc was last touched at SHA X and hasn't
        been touched since," which is uniquely identified by SHA X.

        When SHA X changes (someone finally touched the doc), the signal
        will either stop firing (below threshold now) or fire fresh with
        a new fingerprint. Either way is correct behavior."""
        import hashlib

        ev = result.evidence or {}
        slug = self._evidence_slug()
        last_sha = ev.get(f"last_{slug}_sha")
        exists = ev.get(f"{slug}_exists_in_history", False)
        stable = f"doc-staleness:{slug}:sha={last_sha}:exists={exists}"
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


class D4AgentsMdStaleness(_DocFileStaleness):
    """``AGENTS.md`` untouched while the project has had material churn.

    Fires when ``AGENTS.md`` has not been committed for ≥30 commits AND the
    project itself has had ≥30 total commits. A strict subset of PR2's D2
    (files-touched-not-in-AGENTS.md) — once D2 lands, plan to retire D4."""

    id = "D4"
    doc_path = "AGENTS.md"


class D5ReadmeStaleness(_DocFileStaleness):
    """``README.md`` untouched while the project has had material churn.

    Same shape as D4 but for the human-facing README onboarding now produces
    alongside AGENTS.md (C6 → 1.0). A stale README means the project's
    stated purpose + quickstart has drifted from what the codebase now is —
    exactly the doc-drift signal C6 was set up to catch."""

    id = "D5"
    doc_path = "README.md"


class D6ArchitectureStaleness(_DocFileStaleness):
    """``ARCHITECTURE.md`` untouched while the project has had material churn.

    Onboarding produces ARCHITECTURE.md as the "how it fits together" doc.
    A stale one is drift between the described boundaries and what the
    code now actually does — the compounding hazard C6/C8 exist to prevent."""

    id = "D6"
    doc_path = "ARCHITECTURE.md"


class D7DecisionsStaleness(_DocFileStaleness):
    """``DECISIONS.md`` untouched while the project has had material churn.

    Onboarding produces DECISIONS.md as the ADR-style rationale log. Not
    firing this signal for a stable, well-thought-through project is
    correct behavior — a project MAY have all its decisions captured. Firing
    it after 30 commits means changes are landing without their reasoning
    getting written down. Same doc-drift shape as D4/D5/D6."""

    id = "D7"
    doc_path = "DECISIONS.md"


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


# ---- bookmark-aware drift signals (PR2) ------------------------------------


_SHORTSTAT_RE = re.compile(
    r"(\d+)\s+files?\s+changed"
    r"(?:,\s+(\d+)\s+insertions?\(\+\))?"
    r"(?:,\s+(\d+)\s+deletions?\(-\))?"
)


def _parse_shortstat(out: str) -> tuple[int, int, int]:
    """Parse the last non-empty line of ``git diff --shortstat`` output.
    Returns ``(files, insertions, deletions)``; ``(0, 0, 0)`` if nothing
    matches (empty output, parse miss). Robust to the ``shortstat`` shape
    omitting insertions OR deletions when one side is zero."""
    for line in reversed(out.splitlines()):
        s = line.strip()
        if not s:
            continue
        m = _SHORTSTAT_RE.search(s)
        if m:
            files = int(m.group(1))
            insertions = int(m.group(2)) if m.group(2) else 0
            deletions = int(m.group(3)) if m.group(3) else 0
            return files, insertions, deletions
    return 0, 0, 0


class D1DiffVolume(Signal):
    """Volume of change since the detector last observed this workspace.

    Reads ``git diff --shortstat <trend-bookmark>..HEAD`` and fires when ≥10
    files OR ≥500 lines have changed since the last observation. Doesn't
    distinguish good vs bad change — that's the LLM retrospective's job.
    Advances the bookmark on fire so the next observation starts fresh."""

    id = "D1"
    category = "drift"
    scope = "per_project"
    advances_bookmark = True

    files_threshold = 10
    lines_threshold = 500

    def check(self, ctx: SignalContext) -> SignalResult:
        if not ctx.workspace_dir or not ctx.bookmark:
            # No bookmark yet → detector seeded it this heartbeat; defer.
            return SignalResult(fired=False)
        out = _run_git(
            ["diff", "--shortstat", f"{ctx.bookmark}..HEAD"],
            cwd=ctx.workspace_dir,
        )
        if not out.strip():
            return SignalResult(fired=False)  # bookmark == HEAD or git error
        files, insertions, deletions = _parse_shortstat(out)
        lines_changed = insertions + deletions
        fired = files >= self.files_threshold or lines_changed >= self.lines_threshold
        # actual value reports max ratio against thresholds — gives the LLM
        # a sense of how badly we crossed.
        actual = max(
            float(files) / self.files_threshold,
            float(lines_changed) / self.lines_threshold,
        )
        if not fired:
            return SignalResult(
                fired=False, actual_value=actual, threshold_value=1.0,
            )
        return SignalResult(
            fired=True, actual_value=actual, threshold_value=1.0,
            evidence={
                "files_changed": files,
                "lines_changed": lines_changed,
                "insertions": insertions,
                "deletions": deletions,
                "since_bookmark": ctx.bookmark[:12],
            },
            deeper_refs={
                "git_diff_cmd": f"git diff --stat {ctx.bookmark}..HEAD",
            },
        )


class D2FilesNotInAgentsMd(Signal):
    """Files touched repeatedly since the bookmark that AGENTS.md doesn't mention.

    Reads ``git log --name-only <bookmark>..HEAD`` for the set of touched
    files, tallies per-file commit counts, then checks AGENTS.md text for a
    substring match (path or directory). Fires when ≥3 distinct files
    touched ≥2 commits each are not referenced. Skips when AGENTS.md doesn't
    exist (that's D4's concern — keep the signals orthogonal)."""

    id = "D2"
    category = "drift"
    scope = "per_project"
    advances_bookmark = True

    files_threshold = 3
    min_commit_count_per_file = 2

    def check(self, ctx: SignalContext) -> SignalResult:
        if not ctx.workspace_dir or not ctx.bookmark:
            return SignalResult(fired=False)
        agents_md = Path(ctx.workspace_dir) / "AGENTS.md"
        if not agents_md.is_file():
            return SignalResult(fired=False)
        try:
            agents_text = agents_md.read_text()
        except OSError:
            return SignalResult(fired=False)
        out = _run_git(
            ["log", "--pretty=format:%H", "--name-only", "--no-merges",
             f"{ctx.bookmark}..HEAD"],
            cwd=ctx.workspace_dir,
        )
        entries = _parse_git_log_name_only(out)
        per_file: dict[str, int] = {}
        for _sha, files in entries:
            for f in files:
                per_file[f] = per_file.get(f, 0) + 1
        # Files touched ≥N times that AGENTS.md doesn't mention (substring).
        unreferenced: list[tuple[str, int]] = []
        for f, n in per_file.items():
            if n < self.min_commit_count_per_file:
                continue
            if _path_or_parent_in_text(f, agents_text):
                continue
            unreferenced.append((f, n))
        if len(unreferenced) < self.files_threshold:
            return SignalResult(
                fired=False,
                actual_value=float(len(unreferenced)),
                threshold_value=float(self.files_threshold),
            )
        unreferenced.sort(key=lambda fn: -fn[1])
        return SignalResult(
            fired=True,
            actual_value=float(len(unreferenced)),
            threshold_value=float(self.files_threshold),
            evidence={
                "unreferenced_files_count": len(unreferenced),
                "top_files": [
                    {"file": f, "commit_count": n} for f, n in unreferenced[:8]
                ],
                "since_bookmark": ctx.bookmark[:12],
            },
            deeper_refs={
                "git_log_cmd": (
                    f"git log --name-only {ctx.bookmark}..HEAD"
                ),
                "agents_md_path": str(agents_md),
            },
        )


def _path_or_parent_in_text(path: str, text: str) -> bool:
    """True if ``path`` (or one of its parent directories) appears as a
    substring in ``text``. Matches against backslash-or-slash-separated
    fragments, so ``src/foo/bar.py`` finds a reference of ``src/foo`` /
    ``foo/bar.py`` / ``foo`` in AGENTS.md too. False positives are fine —
    we want generous matching to avoid spurious 'unreferenced' fires."""
    if not path:
        return False
    # Try the full path AND each progressively-shorter prefix.
    parts = path.split("/")
    candidates: set[str] = {path}
    for i in range(1, len(parts)):
        candidates.add("/".join(parts[:i]))
        candidates.add("/".join(parts[i:]))
    # Also bare filename so ``trend_signals.py`` matches as a backstop.
    candidates.add(parts[-1])
    return any(c and c in text for c in candidates if len(c) >= 3)


class D3NewArchitecturalSurface(Signal):
    """A new top-level (or depth-2) directory was added, OR a new external
    dependency landed.

    Reads ``git diff --diff-filter=A --name-only <bookmark>..HEAD`` for newly
    added paths and groups by depth-1/2 directory. Reads per-format dep file
    diffs (``requirements.txt`` / ``pyproject.toml`` / ``package.json``) for
    added dep lines. Fires on either. Both are architectural events worth
    noting; the LLM judges which matter."""

    id = "D3"
    category = "drift"
    scope = "per_project"
    advances_bookmark = True

    _DEP_FILES = ("requirements.txt", "pyproject.toml", "package.json")

    def check(self, ctx: SignalContext) -> SignalResult:
        if not ctx.workspace_dir or not ctx.bookmark:
            return SignalResult(fired=False)
        new_paths_raw = _run_git(
            ["diff", "--diff-filter=A", "--name-only",
             f"{ctx.bookmark}..HEAD"],
            cwd=ctx.workspace_dir,
        )
        new_paths = [p for p in new_paths_raw.splitlines() if p.strip()]
        new_dirs = _new_top_level_dirs(new_paths)

        new_dep_files: dict[str, int] = {}
        for dep_file in self._DEP_FILES:
            if not (Path(ctx.workspace_dir) / dep_file).is_file():
                continue
            diff = _run_git(
                ["diff", "--unified=0",
                 f"{ctx.bookmark}..HEAD", "--", dep_file],
                cwd=ctx.workspace_dir,
            )
            count = _count_added_dep_lines(diff)
            if count > 0:
                new_dep_files[dep_file] = count

        if not new_dirs and not new_dep_files:
            return SignalResult(
                fired=False,
                actual_value=0.0,
                threshold_value=1.0,
            )

        evidence: dict = {"since_bookmark": ctx.bookmark[:12]}
        if new_dirs:
            evidence["new_directories"] = sorted(new_dirs)[:8]
        if new_dep_files:
            evidence["new_deps_per_file"] = new_dep_files
        return SignalResult(
            fired=True,
            actual_value=float(len(new_dirs) + sum(new_dep_files.values())),
            threshold_value=1.0,
            evidence=evidence,
            deeper_refs={
                "git_diff_cmd": (
                    f"git diff --diff-filter=A --name-only "
                    f"{ctx.bookmark}..HEAD"
                ),
            },
        )


def _new_top_level_dirs(new_paths: list[str]) -> set[str]:
    """Top-level (depth-1) directories present in the added paths list. A new
    depth-1 directory matters more than a new deeply-nested file; we surface
    only the depth-1 grouping. (Depth-2 surfacing can be added if PR2's first
    backtest shows we need it.)"""
    out: set[str] = set()
    for p in new_paths:
        parts = p.split("/")
        if len(parts) >= 2 and parts[0]:  # has at least one directory
            out.add(parts[0])
    return out


def _count_added_dep_lines(diff: str) -> int:
    """Count + lines (excluding +++ headers) in a dep-file unified diff. Crude
    on purpose — we don't try to parse TOML/JSON/requirements semantics
    because the LLM retrospective distinguishes "new dep" from "version bump"
    from "comment". The pre-filter just notices the dep file changed."""
    count = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            # Skip pure-whitespace and pure-comment additions.
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            count += 1
    return count


# ---- registry --------------------------------------------------------------


def all_signals() -> list[Signal]:
    """The current signal set. PR3/4/5 extend further. Order doesn't determine
    fire priority (the orchestrator owns that — see
    ``trend_detector._SIGNAL_PRIORITY``)."""
    return [
        R2RepeatedFixHotspot(),
        D1DiffVolume(),
        D2FilesNotInAgentsMd(),
        D3NewArchitecturalSurface(),
        D4AgentsMdStaleness(),
        D5ReadmeStaleness(),
        D6ArchitectureStaleness(),
        D7DecisionsStaleness(),
        H4SteeringFrequency(),
    ]
