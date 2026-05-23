"""Truly-atomic PR check — gates the auto-merge path.

Auto-merge is the riskiest action devclaw takes. A PR that *looks* small
can still be load-bearing (a schema change, a public API rename, a CI
workflow tweak) and must never be merged without human review. This module
encodes the 8-rule allowlist from
`proposals/2026-05-20-atomic-merge-rule-tightening.md` — every rule has to
pass for `is_truly_atomic` to return True.

Thresholds and pattern lists live in
`orchestrator/config/atomic_rules.yaml` so the policy can be tuned
without a code change.

Usage:

    ok, reason = is_truly_atomic({
        "files": [{"path": "...", "additions": 10}, ...],
        "author": "ci_failure_dispatcher",
    })
    if not ok:
        # surface for review; do not auto-merge
        ...
"""

from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "config" / "atomic_rules.yaml"


# ─── Glob matching with ** support ─────────────────────────────────────────


def _glob_to_regex(pattern: str) -> str:
    """Convert a glob pattern (with `**` recursive matching) to an anchored regex.

    `**` matches zero or more path components; `*` matches anything except `/`;
    `?` matches any single non-`/` character. Other regex metacharacters are
    escaped.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        # **/  → optional any-depth prefix
        if pattern[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
            continue
        # /**/ → /(?:.*/)?
        if pattern[i : i + 4] == "/**/":
            out.append("/(?:.*/)?")
            i += 4
            continue
        # trailing /** → /.+ (require at least one more path char)
        if pattern[i : i + 3] == "/**" and i + 3 == n:
            out.append("/.+")
            i += 3
            continue
        # bare ** (no surrounding slashes) → .*
        if pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in r".+()[]{}|^$\\":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "^" + "".join(out) + "$"


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(_glob_to_regex(pattern))


def _match_path(path: str, pattern: str) -> bool:
    """Match one path against one glob. Bare-name patterns (no `/`) also match
    the path's basename so e.g. `package.json` catches `app/package.json`."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return _compiled(pattern).match(path) is not None


def _matches_any(path: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if _match_path(path, pattern):
            return pattern
    return None


def _any_path_matches(paths: list[str], patterns: list[str]) -> tuple[str, str] | None:
    for path in paths:
        hit = _matches_any(path, patterns)
        if hit:
            return path, hit
    return None


# ─── Config loading ────────────────────────────────────────────────────────


def load_rules(config_path: Path | str | None = None) -> dict[str, Any]:
    """Load the rule thresholds + pattern lists from atomic_rules.yaml."""
    path = Path(config_path) if config_path else CONFIG_PATH
    return yaml.safe_load(path.read_text()) or {}


# ─── Main entry point ──────────────────────────────────────────────────────


def is_truly_atomic(
    pr_metadata: dict[str, Any],
    *,
    rules: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return (True, "") if the PR satisfies all 8 atomic-merge rules.

    Returns (False, reason) on the first rule that disqualifies it. The
    reason string is intended to be surfaced as a PR comment and to land in
    the pr_review telemetry log.

    `pr_metadata` schema (mirrors what `gh pr view --json files,author`
    returns; extra keys are ignored):

        {
          "files": [{"path": str, "additions": int (optional)}, ...],
          # alternatively, "files_changed": [str, ...] of paths
          "author": str,  # GitHub login of the PR opener
        }
    """
    if rules is None:
        rules = load_rules()

    files = pr_metadata.get("files")
    if files is None:
        files = [{"path": p} for p in pr_metadata.get("files_changed", [])]
    files = list(files)
    paths = [str(f.get("path", "")) for f in files if f.get("path")]
    author = pr_metadata.get("author") or ""

    # Rule 8 — Source allowlist (cheap; check first to short-circuit non-bot PRs)
    allowlisted = list(rules.get("allowlisted_dispatchers") or [])
    if author not in allowlisted:
        return False, (
            f"rule 8 (source allowlist): author '{author}' is not in the dispatcher "
            f"allowlist {allowlisted}"
        )

    # Rule 1 — File count
    max_files = int(rules.get("max_files_changed", 3))
    if len(paths) > max_files:
        return False, (
            f"rule 1 (file count): {len(paths)} files changed exceeds cap of {max_files}"
        )

    # Rule 2 — Public API surface
    hit = _any_path_matches(paths, list(rules.get("public_api_patterns") or []))
    if hit:
        return False, (
            f"rule 2 (public API): file '{hit[0]}' matches public-API pattern '{hit[1]}'"
        )

    # Rule 3 — Schema change
    hit = _any_path_matches(paths, list(rules.get("schema_patterns") or []))
    if hit:
        return False, (
            f"rule 3 (schema): file '{hit[0]}' matches schema pattern '{hit[1]}'"
        )

    # Rule 4 — Dependency change
    hit = _any_path_matches(paths, list(rules.get("dep_patterns") or []))
    if hit:
        return False, (
            f"rule 4 (dependencies): file '{hit[0]}' matches dependency pattern '{hit[1]}'"
        )

    # Rule 5 — CI / infra
    hit = _any_path_matches(paths, list(rules.get("ci_infra_patterns") or []))
    if hit:
        return False, (
            f"rule 5 (CI/infra): file '{hit[0]}' matches CI/infra pattern '{hit[1]}'"
        )

    # Rule 6 — Security-sensitive paths
    hit = _any_path_matches(paths, list(rules.get("security_patterns") or []))
    if hit:
        return False, (
            f"rule 6 (security): file '{hit[0]}' matches security-sensitive pattern '{hit[1]}'"
        )

    # Rule 7 — Diff size (added lines, excluding tests)
    max_added = int(rules.get("max_added_lines", 150))
    test_patterns = list(rules.get("test_patterns") or [])
    non_test_added = 0
    for f in files:
        path = str(f.get("path", ""))
        if not path:
            continue
        if _matches_any(path, test_patterns):
            continue
        non_test_added += int(f.get("additions") or 0)
    if non_test_added > max_added:
        return False, (
            f"rule 7 (diff size): {non_test_added} non-test lines added exceeds cap "
            f"of {max_added}"
        )

    return True, ""
