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

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..llm_call import PlannerError, claude_with_model, extract_json

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


# ---------------------------------------------------------------------------
# Review panel — diverse-lens fan-out (opt-in via DEVCLAW_REVIEW_PANEL_N).
#
# One reviewer is correlated with itself: N copies of the same prompt miss the
# same defects. The panel's value is DIVERSE LENSES — each panelist reads the
# same diff under a distinct emphasis, so a bug one lens is blind to another
# catches. The base review-gate prompt (with its #227 grounding clause) is
# ALWAYS the spine; a lens only ADDS a focus block, never removes the grounding.
# ---------------------------------------------------------------------------

#: Distinct review emphases, in priority order. For N panelists we take the
#: first N (round-robin if N exceeds the list). `meets_acceptance_criteria`
#: leverages the per-task acceptance criteria carried in the goal/ticket string
#: (shape B, #252).
_REVIEW_LENSES: tuple[str, ...] = (
    "correctness",
    "regression_risk",
    "meets_acceptance_criteria",
)

_LENS_INSTRUCTIONS: dict[str, str] = {
    "correctness": (
        "PANEL LENS — CORRECTNESS. You are one reviewer on an adversarial panel; "
        "concentrate your scrutiny on CORRECTNESS: logic errors, off-by-one and "
        "boundary mistakes, null/empty/error-path handling, incorrect API or "
        "contract use, race conditions, and any path on which the change produces "
        "a wrong result. Still report other issues you notice, but hunt hardest here."
    ),
    "regression_risk": (
        "PANEL LENS — REGRESSION RISK. You are one reviewer on an adversarial "
        "panel; concentrate your scrutiny on REGRESSION RISK: behaviour this change "
        "could break OUTSIDE the diff — altered shared contracts, removed or renamed "
        "symbols still referenced elsewhere, changed defaults, side effects on "
        "unrelated call sites, and behaviour the passing gate does not exercise. "
        "Still report other issues you notice, but hunt hardest here."
    ),
    "meets_acceptance_criteria": (
        "PANEL LENS — ACCEPTANCE CRITERIA. You are one reviewer on an adversarial "
        "panel; concentrate your scrutiny on whether the change MEETS THE TICKET'S "
        "ACCEPTANCE CRITERIA: read the acceptance criteria / done-when conditions "
        "carried in the ticket above and verify each one is actually satisfied by "
        "the diff. A criterion left unmet, stubbed, or only partially implemented is "
        "a blocker. Still report other issues you notice, but hunt hardest here."
    ),
}


def build_review_prompt(
    *,
    goal: str,
    kind: str,
    diff: str,
    repo_context: Optional[str] = None,
    lens: Optional[str] = None,
) -> str:
    from ..prompts import load_prompt

    parts = [load_prompt("review-gate")]
    # A lens only ADDS a focus block after the base contract — the grounding
    # clause and the two-axis hunt in review-gate.md stay intact for every
    # panelist. `lens is None` (the default / single-reviewer path) is
    # byte-identical to the pre-panel prompt.
    if lens is not None:
        instruction = _LENS_INSTRUCTIONS.get(lens)
        if instruction:
            parts.append(instruction)
    parts.append(f"TICKET ({kind}):\n{goal}")
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


# ---------------------------------------------------------------------------
# Review panel — durable, stateful analog of an ephemeral adversarial fan-out.
# Opt-in via DEVCLAW_REVIEW_PANEL_N (default 1 = today's single reviewer).
# ---------------------------------------------------------------------------

def _panel_n() -> int:
    """Panelist count from ``DEVCLAW_REVIEW_PANEL_N``, clamped to >=1. Default 1
    keeps the gate byte-identical to the single-reviewer behaviour until an
    operator opts in. Unparseable / <1 → 1 (never zero reviewers)."""
    raw = os.environ.get("DEVCLAW_REVIEW_PANEL_N", "1")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _lenses_for(n: int) -> list[str]:
    """The lens each of ``n`` panelists reviews under. Capped to ``n`` distinct
    lenses; when ``n`` exceeds the lens list they're reused round-robin. (N==1
    never reaches here — it delegates to ``review_diff`` for byte-identity.)"""
    return [_REVIEW_LENSES[i % len(_REVIEW_LENSES)] for i in range(n)]


#: Vote fields NOT wired here mirror ``review_diff``'s per-call cost. The vote
#: is what we persist; the aggregate is a projection over the votes.
@dataclass
class _PanelOutcome:
    lens: str
    result: Optional[dict]      # validated verdict dict, or None for a non-vote
    vote: dict                  # the persisted record (lens/verdict/blocking_count/latency_ms/error)
    raw: Optional[str] = None   # the model's raw response on failure (carries quota prose)


async def _run_panelist(
    *,
    goal: str,
    kind: str,
    diff: str,
    repo_context: Optional[str],
    lens: str,
    claude_caller: Callable[[str], Awaitable[str]],
    record_vote: Optional[Callable[[dict], None]],
) -> _PanelOutcome:
    """One panelist: build the lens prompt, call the model, validate. A crash /
    unparseable output is a NON-VOTE (``result=None``), never an approval — the
    error and the raw response are captured so the aggregate can (a) reach the
    fail-closed quorum decision and (b) preserve any usage-limit prose for the
    queue's quota classifier. The vote is recorded regardless of outcome."""
    started = time.monotonic()
    vote: dict = {
        "lens": lens,
        "verdict": None,
        "blocking_count": 0,
        "latency_ms": None,
        "error": None,
    }
    result: Optional[dict] = None
    raw_response: Optional[str] = None
    try:
        prompt = build_review_prompt(
            goal=goal, kind=kind, diff=diff, repo_context=repo_context, lens=lens
        )
        raw = await claude_caller(prompt)
        try:
            parsed = json.loads(extract_json(raw))
        except json.JSONDecodeError as err:
            raise PlannerError(f"Review JSON parse failed: {err}", raw) from err
        result = validate_review(parsed)
        vote["verdict"] = result["verdict"]
        vote["blocking_count"] = len(result["blocking"])
    except Exception as err:  # noqa: BLE001 — a non-vote, NEVER an approval
        vote["error"] = f"{err.__class__.__name__}: {err}"
        raw_attr = getattr(err, "raw", None)
        if isinstance(raw_attr, str) and raw_attr.strip():
            raw_response = raw_attr
    finally:
        vote["latency_ms"] = int((time.monotonic() - started) * 1000)
        if record_vote is not None:
            try:
                record_vote(dict(vote))
            except Exception:  # noqa: BLE001 — telemetry never breaks the gate
                pass
    return _PanelOutcome(lens=lens, result=result, vote=vote, raw=raw_response)


def _dedup_issues(issues: list[dict]) -> list[dict]:
    """Union issues, deduped by (location, severity) — the first occurrence
    wins. Deterministic order (insertion) so the aggregate is stable."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for issue in issues:
        key = (issue.get("location", ""), issue.get("severity", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def _panel_summary(outcomes: list[_PanelOutcome], verdict: str, n_blocking: int) -> str:
    """One-line human read of the aggregate: verdict, each lens's own verdict."""
    per_lens = ", ".join(
        f"{o.lens}={o.vote.get('verdict')}" for o in outcomes if o.result is not None
    )
    head = (
        f"diverse-lens review panel ({len(outcomes)} reviewers): {verdict}"
        f" — {n_blocking} unioned blocking issue(s)"
    )
    return f"{head}. Lens verdicts: {per_lens}." if per_lens else head


async def _review_panel_core(
    *,
    goal: str,
    kind: str,
    diff: str,
    repo_context: Optional[str] = None,
    claude_caller: Callable[[str], Awaitable[str]] = review_caller,
    record_vote: Optional[Callable[[dict], None]] = None,
    n: Optional[int] = None,
) -> dict:
    """Adversarial review PANEL — a drop-in for ``review_diff`` returning the
    identical ``{verdict, summary, issues, blocking}`` dict shape.

    ``n`` panelists (default ``DEVCLAW_REVIEW_PANEL_N``, default 1) review the
    SAME diff in parallel under DIVERSE LENSES; their blocking issues are unioned
    (evidence wins), so the panel is strictly >= as strict as the single reviewer
    — it can only catch MORE, never ship a bug the single reviewer would have
    caught.

    Fail-CLOSED invariants (never weakened):
      - N==1 is byte-identical to ``review_diff`` today, INCLUDING that an
        unparseable/crashing model RAISES (the queue then fails closed + fast,
        #186) — it never becomes an approval.
      - For N>=2, a panelist crash / unparseable output is a NON-VOTE. If fewer
        than a quorum (``ceil(N/2)``, min 1) of valid votes come back, the panel
        RAISES ``PlannerError`` — mirroring #186 (unreviewable ⇒ fail closed AND
        fast, no futile agent retry). A crash NEVER yields ``approve``. The raise
        carries the panelists' raw responses so a session-limit sub-quorum is
        classified as quota by the queue and PAUSES rather than fails (#245).
    """
    if n is None:
        n = _panel_n()
    n = max(1, n)

    # N==1 → the single generic reviewer, unchanged. ``review_diff`` owns the
    # empty/generated short-circuit AND the fail-closed raise on a bad verdict,
    # so this branch is byte-identical to the pre-panel gate.
    if n == 1:
        return await review_diff(
            goal=goal, kind=kind, diff=diff,
            repo_context=repo_context, claude_caller=claude_caller,
        )

    # Empty / purely-generated diff short-circuits ONCE, before spawning any
    # panelist (same effect as review_diff's short-circuit; done here so the
    # panel never fans out N model calls over nothing).
    if not filter_reviewable_diff(diff).strip():
        return {
            "verdict": "approve",
            "summary": "no hand-written changes to review "
            "(diff is entirely generated/lock/vendored files)",
            "issues": [],
            "blocking": [],
        }

    lenses = _lenses_for(n)
    outcomes: list[_PanelOutcome] = await asyncio.gather(
        *(
            _run_panelist(
                goal=goal, kind=kind, diff=diff, repo_context=repo_context,
                lens=lens, claude_caller=claude_caller, record_vote=record_vote,
            )
            for lens in lenses
        )
    )

    valid = [o for o in outcomes if o.result is not None]
    quorum = max(1, math.ceil(n / 2))
    if len(valid) < quorum:
        # Fail CLOSED + FAST: the panel could not produce a trustworthy verdict.
        # RAISE (not return request_changes) so the queue's crash path fails the
        # task WITHOUT an agent retry — re-running reproduces the same diff and
        # re-crashes identically (#186 unreviewable-fails-closed-and-fast). Carry
        # every panelist's raw response so a usage-limit sub-quorum is classified
        # as quota and PAUSES rather than being read as a permanent defect (#245).
        errs = [o.vote["error"] for o in outcomes if o.vote.get("error")]
        raws = [o.raw for o in outcomes if o.raw]
        msg = (
            f"review panel could not reach quorum — {len(valid)} of {n} reviewers "
            f"produced a valid verdict (needed {quorum}); reviewer errors: "
            f"{' | '.join(errs) if errs else 'none'}. The diff was not reliably "
            "reviewed, so it must not ship on the panel's silence — split it into "
            "smaller commits or review it by hand."
        )
        raise PlannerError(msg, "\n".join(raws) if raws else None)

    # Evidence-wins union: every valid panelist's issues, deduped by
    # (location, severity). Blocking is the blocker/major subset — so a single
    # panelist's blocker forces request_changes (>= today's strictness).
    merged_issues = _dedup_issues([i for o in valid for i in o.result["issues"]])
    blocking = [i for i in merged_issues if i["severity"] in ("blocker", "major")]
    verdict = "request_changes" if blocking else "approve"
    return {
        "verdict": verdict,
        "summary": _panel_summary(outcomes, verdict, len(blocking)),
        "issues": merged_issues,
        "blocking": blocking,
    }


# ---------------------------------------------------------------------------
# Cognition-TIMEOUT degradation ladder (systemic fix #5).
#
# The review model gets a fixed per-call budget (REVIEW_TIMEOUT_MS). On a large-
# but-legitimate diff it can exhaust that budget, the caller raises a timeout
# PlannerError, and the task fails CLOSED with no agent retry (#186 — re-running
# reproduces the same over-large diff and re-times-out identically). Correct, but
# it gives up on the WHOLE diff without trying anything cheaper first. This ladder
# adds ONE degradation rung *before* that hard fail: when the full-diff review
# times out, split the diff into one sub-diff PER FILE and review each
# independently, then UNION the verdicts with the exact evidence-wins semantics
# the panel already uses (a single sub-review's blocker forces request_changes).
# Each per-file review is smaller, so it fits the budget where the whole diff did
# not — a legitimate large diff can still earn a real verdict.
#
# Fail-closed is preserved end to end:
#   - Trigger is TIMEOUT ONLY (the documented symptom). An unparseable-verdict
#     crash re-raises unchanged — re-running per file reproduces the same
#     unparseable output, so degrading it is futile; it stays fail-closed + fast.
#   - Each per-file sub-review STILL fails closed: a sub-review that times out or
#     can't be parsed RAISES, which propagates out of the ladder → the whole diff
#     fails closed (never an approval), carrying its raw response so a quota-shaped
#     sub-failure is still classified as quota by the queue and PAUSES.
#   - When the ladder can't help (a single unsplittable file still times out, or
#     the diff has more files than the fan-out cap), it RE-RAISES the original
#     timeout → the same crash-marker, no-agent-retry path (#186). Degradation
#     NEVER manufactures a passing verdict.
#
# Opt-out via DEVCLAW_REVIEW_DEGRADE=0 (then a timeout re-raises immediately,
# byte-identical to the pre-ladder gate). The per-file fan-out is bounded by
# DEVCLAW_REVIEW_DEGRADE_MAX_FILES so a pathologically wide diff can't spray
# hundreds of model calls — over the cap it fails closed and a human splits it.
# ---------------------------------------------------------------------------

#: Default per-file fan-out cap. A diff with more reviewable files than this is
#: NOT degraded (the fan-out would be too large a burst); it fails closed and the
#: owner splits the commit. Env-tunable via DEVCLAW_REVIEW_DEGRADE_MAX_FILES.
_DEGRADE_MAX_FILES_DEFAULT = 40


def _degrade_enabled() -> bool:
    """Whether the timeout degradation ladder runs. Default ON; an operator opts
    out with ``DEVCLAW_REVIEW_DEGRADE=0`` (or false/no/off), which restores the
    pre-ladder behaviour exactly (a review timeout re-raises immediately)."""
    raw = os.environ.get("DEVCLAW_REVIEW_DEGRADE", "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _degrade_max_files() -> int:
    """Per-file fan-out cap from ``DEVCLAW_REVIEW_DEGRADE_MAX_FILES``, clamped to
    >=1. Unparseable / <1 → the default. Above the cap the ladder declines to
    degrade and the diff fails closed."""
    raw = os.environ.get("DEVCLAW_REVIEW_DEGRADE_MAX_FILES", "")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEGRADE_MAX_FILES_DEFAULT
    return max(1, v)


def _is_review_timeout(err: Exception) -> bool:
    """True iff ``err`` is a cognition TIMEOUT — the documented degradation
    trigger. Matches the planner's timeout message ("claude --print timed out
    after ...ms") and the panel's sub-quorum raise when panelist timeouts drove
    it. An unparseable-verdict crash is deliberately NOT a trigger."""
    return isinstance(err, PlannerError) and "timed out" in str(err).lower()


def _split_diff_by_file(diff: str) -> list[str]:
    """Split a unified diff into one sub-diff per ``diff --git`` block, so an
    over-large diff that timed out as a whole can be reviewed file-by-file. Any
    preamble before the first header is prepended to the first block so nothing is
    dropped. A diff with no header (or blank) yields at most one element — the
    caller then can't degrade and fails closed. Expects an already
    reviewable-filtered diff (generated/lock/vendored blocks removed)."""
    if not diff.strip():
        return []
    if "diff --git " not in diff:
        return [diff]
    preamble: list[str] = []
    blocks: list[list[str]] = []
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
    texts = ["".join(b) for b in blocks]
    pre = "".join(preamble)
    if pre.strip() and texts:
        texts[0] = pre + texts[0]
    return texts


def _aggregate_file_reviews(results: list[dict], *, n_files: int) -> dict:
    """Union per-file sub-review verdicts into one, with the SAME evidence-wins
    semantics the panel uses: issues are unioned + deduped by (location,
    severity), the blocking subset is the blocker/major issues, and the verdict is
    request_changes iff any blocking issue exists. So a single file's blocker
    still forces request_changes.

    IMPORTANT — this is NOT strictly >= as strict as a whole-diff review: reviewing
    each file in ISOLATION loses cross-file context, so a regression that only
    shows up across files (a symbol renamed in one file but still referenced in
    another — the ``regression_risk`` lens's target) can pass per-file where a
    whole-diff review would have blocked it. This is an accepted thoroughness
    trade-off, engaged ONLY on a path that otherwise hard-fails the diff outright:
    a degraded real verdict on most of the diff beats no verdict at all, and every
    fail-closed guarantee (a sub-review that can't produce a verdict still raises →
    the whole diff fails closed) is preserved."""
    merged_issues = _dedup_issues([i for r in results for i in r.get("issues", [])])
    blocking = [i for i in merged_issues if i["severity"] in ("blocker", "major")]
    verdict = "request_changes" if blocking else "approve"
    summary = (
        f"degraded per-file review — the full diff exceeded the review budget, so "
        f"it was reviewed as {n_files} per-file sub-diffs and their verdicts "
        f"unioned: {verdict} ({len(blocking)} blocking issue(s))."
    )
    return {
        "verdict": verdict,
        "summary": summary,
        "issues": merged_issues,
        "blocking": blocking,
    }


async def review_panel(
    *,
    goal: str,
    kind: str,
    diff: str,
    repo_context: Optional[str] = None,
    claude_caller: Callable[[str], Awaitable[str]] = review_caller,
    record_vote: Optional[Callable[[dict], None]] = None,
    n: Optional[int] = None,
) -> dict:
    """The wired review entry — the diverse-lens panel (:func:`_review_panel_core`)
    wrapped in the cognition-timeout degradation ladder.

    The happy path is byte-identical to the panel: this just returns
    ``_review_panel_core(...)``. Only when that call raises a TIMEOUT does the
    ladder engage — it re-reviews the diff one file at a time and unions the
    verdicts (see the ladder note above). Every fail-closed invariant is
    preserved; the ladder can only turn a whole-diff timeout into a real per-file
    verdict OR fall through to the same fail-closed raise, never into an approval."""
    try:
        return await _review_panel_core(
            goal=goal, kind=kind, diff=diff, repo_context=repo_context,
            claude_caller=claude_caller, record_vote=record_vote, n=n,
        )
    except PlannerError as err:
        # Only a TIMEOUT triggers the ladder, and only when enabled. Anything else
        # (an unparseable verdict, a quota-shaped crash) re-raises UNCHANGED so the
        # queue's fail-closed / quota-classify paths see it exactly as before.
        if not _degrade_enabled() or not _is_review_timeout(err):
            raise
        sub_diffs = _split_diff_by_file(filter_reviewable_diff(diff))
        # Can't split further (0 or 1 reviewable file) → nothing cheaper to try →
        # re-raise the ORIGINAL timeout so the diff fails closed on the same
        # crash-marker, no-agent-retry path (#186).
        if len(sub_diffs) <= 1:
            raise
        # Too many files to fan out safely → decline to degrade (a burst of that
        # many model calls is its own hazard) and fail closed; a human splits it.
        if len(sub_diffs) > _degrade_max_files():
            raise
        # Review each file's sub-diff independently through the SAME panel core.
        # A sub-review that RAISES (still times out on one huge file, or
        # unparseable) must propagate straight out of the ladder → the whole diff
        # fails closed (never approved), carrying its raw response for the queue's
        # quota classifier. On that first failure we CANCEL the still-running
        # siblings: the ladder has already decided to fail closed, so leaving the
        # other per-file `claude` calls running only burns OAuth quota. (Plain
        # gather raises but ORPHANS the siblings — hence explicit tasks + cancel.)
        tasks = [
            asyncio.ensure_future(
                _review_panel_core(
                    goal=goal, kind=kind, diff=sub, repo_context=repo_context,
                    claude_caller=claude_caller, record_vote=record_vote, n=n,
                )
            )
            for sub in sub_diffs
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            # Let the cancellations settle (swallow their CancelledError/results)
            # before re-raising the original sub-review failure.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return _aggregate_file_reviews(results, n_files=len(sub_diffs))
