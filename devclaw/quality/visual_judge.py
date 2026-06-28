"""Pre-PR visual-evidence gate — make "green tests" mean "looks like product".

The verify gate proves the change *behaves* (tests pass) and the diff-review gate
proves the *code* is sound, but neither one ever *sees the rendered UI*. That's
the hole the live CRM project keeps falling through: green tests, broken layout,
"not production ready". This module closes it. After the gate + integrity pass
and BEFORE the diff-review gate runs, a multimodal Claude pass reads the
screenshots captured by the project's ``.agent/visual-verify.sh`` script against
a universal rubric + the per-repo ``.agent/visual-rubric.md``, and returns a
structured verdict. On ``request_changes`` the task queue feeds the issues back
into the existing retry loop exactly like a gate failure.

Same shape as the diff-review gate (``devclaw.quality.review_diff``): cognition
is ``claude`` (host-side, OAuth, no API key), tiered via
``DEVCLAW_VISUAL_JUDGE_MODEL``; the prompt-building and response validation are
pure, so this is unit-testable with a stubbed caller. Screenshots are embedded
as ``@/abs/path.png`` tokens in the prompt text — ``claude --print`` consumes
these as multimodal inputs without a Cognition protocol extension. The trace
recorder logs the prompt text (paths visible) but not the raw image bytes; a
forensic reviewer reopens the screenshots off disk via the manifest.
"""

from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

from ..planner import PlannerError, claude_with_model, extract_json

#: Vision-judging is multimodal — Sonnet is the floor (Haiku's vision is weak;
#: Opus is overkill for screenshot triage). Empty → account default.
VISUAL_JUDGE_MODEL = os.environ.get("DEVCLAW_VISUAL_JUDGE_MODEL", "sonnet") or None
#: default cognition caller for the visual judge, bound to its tier
visual_caller = claude_with_model(VISUAL_JUDGE_MODEL, role="visual_judge")

#: cap how many screenshots ride into one judge call — large UIs could otherwise
#: blow the multimodal prompt cost. When the manifest exceeds this we send the
#: first N entries and annotate the trim in the prompt.
_MAX_SCREENSHOTS = int(os.environ.get("DEVCLAW_VISUAL_MAX_SCREENSHOTS", "8"))
#: same diff-clip posture as the diff-review gate so a huge change can't blow the
#: prompt; head-keep because the head is where the substantive files usually are.
_MAX_DIFF_CHARS = int(os.environ.get("DEVCLAW_VISUAL_MAX_DIFF_CHARS", "30000"))

_SEVERITIES = ("blocker", "major", "minor")


def _clip_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return (
        diff[:_MAX_DIFF_CHARS]
        + f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} chars; review what is shown ...]"
    )


def merge_rubric(per_repo: str) -> str:
    """Compose the per-repo rubric block injected into the prompt. The universal
    rubric is baked into ``visual-judge.md``; this only wraps the project block
    so a missing per-repo rubric is a clean empty slot (not a literal ``None``).
    """
    block = (per_repo or "").strip()
    if not block:
        return "(no per-repo rubric — judge against the universal rubric alone)"
    return f"PER-REPO RUBRIC (from .agent/visual-rubric.md):\n{block}"


def _render_manifest(manifest: list[dict], evidence_dir: str) -> str:
    """Render the manifest as a route-by-route table with inline ``@path``
    tokens so ``claude --print`` ingests each screenshot. ``evidence_dir`` is
    the host-side absolute path to ``<workspace>/.devclaw-evidence/`` — paths
    in the manifest are joined against it unless they're already absolute."""
    if not manifest:
        return "(no routes captured)"
    items: list[str] = []
    trimmed = manifest[:_MAX_SCREENSHOTS]
    for idx, entry in enumerate(trimmed, start=1):
        label = (entry.get("label") or entry.get("url") or f"route-{idx}").strip()
        url = (entry.get("url") or "").strip()
        screenshot = (entry.get("screenshot") or "").strip()
        if screenshot and not os.path.isabs(screenshot):
            screenshot = os.path.join(evidence_dir, screenshot)
        errs = entry.get("console_errors") or []
        lines = [f"- {label}" + (f"  ({url})" if url else "")]
        if screenshot:
            lines.append(f"  screenshot: @{screenshot}")
        if errs:
            joined = "; ".join(str(e).strip() for e in errs if str(e).strip())
            if joined:
                lines.append(f"  console_errors: {joined}")
        items.append("\n".join(lines))
    body = "\n\n".join(items)
    if len(manifest) > _MAX_SCREENSHOTS:
        body += (
            f"\n\n[... {len(manifest) - _MAX_SCREENSHOTS} additional route(s) "
            f"omitted; judge the routes shown above. The manifest carried "
            f"{len(manifest)} entries total.]"
        )
    return body


def build_visual_prompt(
    *,
    goal: str,
    kind: str,
    diff: str,
    manifest: list[dict],
    evidence_dir: str,
    rubric_per_repo: str = "",
) -> str:
    from ..prompts import load_prompt

    return load_prompt(
        "visual-judge",
        KIND=kind,
        GOAL=goal,
        REPO_RUBRIC=merge_rubric(rubric_per_repo),
        MANIFEST=_render_manifest(manifest, evidence_dir),
        DIFF=_clip_diff(diff),
    )


def validate_visual_verdict(parsed: object) -> dict:
    """Validate + normalize the model's verdict. Mirrors the diff-review gate's
    reconciliation: the issue list is the evidence, so an 'approve' with a
    blocker upgrades to request_changes, and a 'request_changes' with only
    minor issues downgrades to approve (a nit can't trap the agent in retry)."""
    if not isinstance(parsed, dict):
        raise PlannerError("Visual judge response must be a JSON object")
    verdict = parsed.get("verdict")
    if verdict not in ("approve", "request_changes"):
        raise PlannerError(
            f"Visual verdict must be 'approve' or 'request_changes', got {verdict!r}"
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
    final_verdict = "request_changes" if blocking else "approve"
    return {
        "verdict": final_verdict,
        "summary": summary,
        "issues": issues,
        "blocking": blocking,
    }


def format_visual_feedback(review: dict) -> str:
    """Render a request_changes verdict as actionable feedback fed back into the
    retry loop. Phrased so the agent knows the failure was visual (not test or
    code-review) so it knows what to re-exercise."""
    lines = ["visual review requested changes before this can ship:"]
    if review.get("summary"):
        lines.append(review["summary"])
    for i in review.get("blocking", []):
        loc = f" [{i['location']}]" if i.get("location") else ""
        fix = f" — fix: {i['fix']}" if i.get("fix") else ""
        lines.append(f"- ({i['severity']}){loc} {i['problem']}{fix}")
    lines.append(
        "Re-run the affected routes against .agent/visual-verify.sh; address "
        "every blocker/major issue above (do not weaken the manifest to do it), "
        "then re-verify."
    )
    return "\n".join(lines)


async def judge_screenshots(
    *,
    goal: str,
    kind: str,
    diff: str,
    manifest: list[dict],
    evidence_dir: str,
    rubric_per_repo: str = "",
    claude_caller: Callable[[str], Awaitable[str]] = visual_caller,
) -> dict:
    """Judge one capture into a validated verdict dict. ``claude_caller`` is
    injected so tests can stub the subprocess. Raises PlannerError if the model
    returns unparseable/invalid JSON (the caller decides whether to fail open).
    """
    prompt = build_visual_prompt(
        goal=goal, kind=kind, diff=diff,
        manifest=manifest, evidence_dir=evidence_dir,
        rubric_per_repo=rubric_per_repo,
    )
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Visual judge JSON parse failed: {err}", raw) from err
    return validate_visual_verdict(parsed)
