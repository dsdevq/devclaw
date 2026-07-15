"""Pre-PR adversarial diff-review gate — make "green" mean "trustworthy".

The verify gate proves a change *behaves* (tests pass) and the test-integrity
guard proves it didn't go green by gutting the suite — but neither one *reads the
code*. That's the hole a spectator-PO can't cover: a `.Take(0)` dead-code line, a
happy-path-only implementation, logic stuffed in the wrong layer, an untested
frontend change all sail through a green gate. This module closes it: after the
gate passes but BEFORE the PR opens, a separate Claude pass reviews the diff
against the ticket and the production quality bar, and returns a structured
verdict. On `request_changes` the task queue feeds the issues back into the
existing retry loop exactly like a gate failure, escalating to the owner after N.

Same shape as the eval judge / planner: cognition is `claude` (host-side, OAuth,
no API key), tiered via DEVCLAW_REVIEW_MODEL; the prompt-building and response
validation are pure, so this is unit-testable with a stubbed caller.
"""

from __future__ import annotations

import json
import os
import re
from typing import Awaitable, Callable, Optional

from ..planner import PlannerError, claude_with_model, extract_json

#: Adversarial code review is judgment-heavy — Sonnet is the right tier (matches
#: the scope grill; heavier than the Haiku classification judge, lighter than the
#: Opus planner). Empty → account default.
from ..model_tiers import model_for as _model_for
REVIEW_MODEL = _model_for("review")
#: per-call timeout. The review reads a diff up to _MAX_DIFF_CHARS (60 KB) and
#: reasons over the whole thing on Sonnet — it was the one large-input cognition
#: role still on the then-90s global ceiling, so a big diff timed out, failed the
#: gate closed, burned the retry budget, and escalated to the owner (#210). Kept
#: explicit even though the general default (``PLANNER_TIMEOUT_MS``, now 180s and
#: env-tunable via ``DEVCLAW_COGNITION_TIMEOUT_S``) has since caught up — the
#: review's budget is a deliberate role-level decision, not an inherited default.
REVIEW_TIMEOUT_MS = 180_000
#: default cognition caller for the review, bound to the review tier + timeout
review_caller = claude_with_model(REVIEW_MODEL, role="review", timeout_ms=REVIEW_TIMEOUT_MS)

#: cap the diff we send so a huge change can't blow the prompt / quota. Tail-kept
#: would lose the header, so we head-keep (the start of the diff, where the
#: substantive files usually are) and note the truncation.
_MAX_DIFF_CHARS = 60_000

_SEVERITIES = ("blocker", "major", "minor")

def _clip_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return (
        diff[:_MAX_DIFF_CHARS]
        + f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} chars; review what is shown ...]"
    )


# ---------------------------------------------------------------------------
# Generated / lock / vendored filtering.
#
# On closeloop-bench, a "scaffold" step (`ng new`, `dotnet new`) produces a huge,
# mostly-*generated* diff (lockfiles + boilerplate). Sending that whole thing to
# the review model is pointless (a human never wrote it) and dangerous (an
# oversized diff makes the model return non-JSON → the gate crashes). So BEFORE
# clipping/sending we drop whole-file blocks for WELL-KNOWN generated artifacts,
# leaving the reviewer only the hand-written source. Conservative on purpose:
# when in doubt we KEEP the block (better to over-review than skip real code), so
# hand-edited config — package.json, angular.json, *.csproj, tsconfig.json — is
# never stripped.
# ---------------------------------------------------------------------------

#: Path segments whose contents are machine-produced build output / vendored deps
#: — anything *under* one of these directories is generated, not hand-written.
_GENERATED_DIRS = frozenset(
    {"node_modules", "dist", "build", "bin", "obj", ".next", "vendor"}
)
#: Exact filenames that are always machine-generated lockfiles (the ones whose
#: extension isn't a giveaway; the ``*.lock`` suffix rule covers the rest).
_GENERATED_FILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "poetry.lock",
        "composer.lock",
        "Gemfile.lock",
    }
)
#: Filename suffixes that mark a generated/minified/lock artifact.
_GENERATED_SUFFIXES = (".lock", ".min.js", ".min.css")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _is_generated_path(path: str) -> bool:
    """True iff ``path`` is a WELL-KNOWN generated/lock/vendored artifact a human
    never hand-edits. Conservative — only the patterns above match; everything
    else (incl. package.json, angular.json, *.csproj, tsconfig.json) is treated
    as hand-written and KEPT."""
    path = path.strip()
    if not path or path == "/dev/null":
        return False
    parts = path.split("/")
    if any(seg in _GENERATED_DIRS for seg in parts):
        return True
    name = parts[-1]
    if name in _GENERATED_FILES:
        return True
    return name.endswith(_GENERATED_SUFFIXES)


def _block_paths(block: str) -> list[str]:
    """Every file path a single ``diff --git`` block references — both sides of
    the header plus the ``--- a/`` / ``+++ b/`` lines. We drop a block only when
    *every* path it names is generated, so a mixed or ambiguous block is kept."""
    paths: list[str] = []
    for line in block.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if m:
                paths.append(m.group(1))
                paths.append(m.group(2))
        elif line.startswith("--- a/"):
            paths.append(line[len("--- a/"):])
        elif line.startswith("+++ b/"):
            paths.append(line[len("+++ b/"):])
    return paths


def filter_reviewable_diff(diff: str) -> str:
    """Strip whole-file blocks for well-known generated/lock/vendored files from a
    unified git diff, leaving only hand-written source for the reviewer. Blocks
    are split on ``diff --git`` headers; a block is dropped only when every path
    it names is generated (see ``_is_generated_path``) — when in doubt it's KEPT.
    Any preamble before the first ``diff --git`` is preserved. A diff with no
    ``diff --git`` header (or an already-clean one) is returned unchanged."""
    if "diff --git " not in diff:
        return diff

    blocks: list[list[str]] = []
    preamble: list[str] = []
    current: Optional[list[str]] = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append(current)

    kept: list[str] = list(preamble)
    for block in blocks:
        text = "".join(block)
        paths = _block_paths(text)
        # Drop only when we resolved at least one path AND all of them are
        # generated; otherwise keep (unresolved path → keep, real source → keep).
        if paths and all(_is_generated_path(p) for p in paths):
            continue
        kept.append(text)
    return "".join(kept)


def build_review_prompt(
    *, goal: str, kind: str, diff: str, repo_context: Optional[str] = None
) -> str:
    from ..prompts import load_prompt

    parts = [
        load_prompt("review-gate"),
        f"TICKET ({kind}):\n{goal}",
    ]
    if repo_context and repo_context.strip():
        parts.append(
            "REPOSITORY CONTEXT (facts from the task workspace — the source of "
            "truth for repo identity, branch, and which files/dirs exist):\n"
            + repo_context.strip()
        )
    parts.append(f"DIFF UNDER REVIEW:\n{_clip_diff(filter_reviewable_diff(diff))}")
    return "\n\n".join(parts)


def validate_review(parsed: object) -> dict:
    """Validate + normalize the model's review into a verdict dict. Enforces the
    invariant that request_changes ⇔ there is a blocker/major issue, so the
    verdict can't disagree with its own issue list."""
    if not isinstance(parsed, dict):
        raise PlannerError("Review response must be a JSON object")
    verdict = parsed.get("verdict")
    if verdict not in ("approve", "request_changes"):
        raise PlannerError(
            f"Review verdict must be 'approve' or 'request_changes', got {verdict!r}"
        )
    summary = parsed.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    raw_issues = parsed.get("issues")
    issues: list[dict] = []
    if isinstance(raw_issues, list):
        for it in raw_issues:
            if not isinstance(it, dict):
                continue
            sev = it.get("severity")
            sev = sev if sev in _SEVERITIES else "minor"
            issues.append(
                {
                    "severity": sev,
                    "location": str(it.get("location", "")).strip(),
                    "problem": str(it.get("problem", "")).strip(),
                    "fix": str(it.get("fix", "")).strip(),
                }
            )
    blocking = [i for i in issues if i["severity"] in ("blocker", "major")]
    # Reconcile verdict with the issue list — the issues are the evidence, so they
    # win: a "request_changes" with no blocking issue is downgraded; an "approve"
    # that nonetheless lists a blocker/major is upgraded to request_changes.
    final_verdict = "request_changes" if blocking else "approve"
    return {
        "verdict": final_verdict,
        "summary": summary,
        "issues": issues,
        "blocking": blocking,
    }


def format_feedback(review: dict) -> str:
    """Render a request_changes verdict as actionable feedback fed back into the
    retry loop (becomes the task's failure context, like a gate failure)."""
    lines = ["code review requested changes before this can ship:"]
    if review.get("summary"):
        lines.append(review["summary"])
    for i in review.get("blocking", []):
        loc = f" [{i['location']}]" if i.get("location") else ""
        fix = f" — fix: {i['fix']}" if i.get("fix") else ""
        lines.append(f"- ({i['severity']}){loc} {i['problem']}{fix}")
    lines.append(
        "Address every blocker/major issue above (do not weaken tests to do it), "
        "then re-verify."
    )
    return "\n".join(lines)


async def review_diff(
    *,
    goal: str,
    kind: str,
    diff: str,
    repo_context: Optional[str] = None,
    claude_caller: Callable[[str], Awaitable[str]] = review_caller,
) -> dict:
    """Review one diff into a validated verdict dict. ``claude_caller`` is injected
    so tests can stub the subprocess. Raises PlannerError if the model returns
    unparseable/invalid JSON (the caller decides whether to fail open)."""
    # Nothing hand-written to review (a pure generated/lock/vendored diff, e.g. a
    # scaffold step's lockfile churn) → approve/skip gracefully rather than send
    # the model an empty diff. Same effect as the empty-diff short-circuit upstream.
    if not filter_reviewable_diff(diff).strip():
        return {
            "verdict": "approve",
            "summary": "no hand-written changes to review "
            "(diff is entirely generated/lock/vendored files)",
            "issues": [],
            "blocking": [],
        }
    prompt = build_review_prompt(
        goal=goal, kind=kind, diff=diff, repo_context=repo_context
    )
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Review JSON parse failed: {err}", raw) from err
    return validate_review(parsed)
