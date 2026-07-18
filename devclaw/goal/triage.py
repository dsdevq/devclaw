"""Self-triage — the propose-only interceptor on the owner-ping path (slice 1).

Denys is the sole diagnostician: every owner ping lands on him and only he can
resolve it. This layer-3 cognition step sits BEFORE an eligible raw owner ping
goes out and turns "there is a problem" into "here is the problem, a proposed
fix, and how to approve it" — the owner becomes an APPROVER, not an author.

Same mechanism/cognition split as the planner/evaluator: Claude drafts, Python
validates the JSON. It (a) dedupes the problem against the deduplicated
``problems`` catalog and (b) drafts ONE concrete proposed resolution. It is
**propose-only** — it never writes state, never notifies, and never auto-acts;
layer 2 (:mod:`devclaw.goal.tick_context`) owns delivery.

Fail toward the owner: :func:`triage` NEVER raises. Any LLM/parse failure
returns ``None`` so the caller falls back to delivering the original raw ping
unchanged (loud, not silent) — the same best-effort contract as the owner
summarizer. The interceptor only ever runs on a REAL problem (an alert that
actually fired), never on an idle tick, so the zero-token idle guard is intact.

Slice 1 wires exactly one trigger — the DB-size alarm (#271); the seam is
general so a future ``needs_answer`` wire is one allowlist entry away.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

ClaudeCaller = Callable[[str], Awaitable[str]]

_VALID_CONFIDENCE = {"high", "medium", "low"}

#: the self-triage model tier (bounded JSON, dedupe + propose a minimal fix →
#: the standard judgment tier, same as goal_planner/goal_eval).
from ..model_tiers import model_for as _model_for
TRIAGE_MODEL = _model_for("triage")


@dataclass(frozen=True)
class TriageProposal:
    """Parsed, validated triage output. Pure data — layer 2 renders + delivers."""

    proposed_fix: str
    approve_hint: str = ""
    dedupe_note: str = ""
    is_duplicate: bool = False
    confidence: str = "low"


class TriageError(Exception):
    """Raised INTERNALLY by validate/parse; :func:`triage` swallows it (best-effort)."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def build_prompt(problem: str, catalog: str, repo_context: str) -> str:
    from ..prompts import load_prompt

    return load_prompt(
        "self-triage",
        problem=problem.strip() or "(no message)",
        catalog=catalog.strip() or "(catalog empty — no prior problems recorded)",
        repo_context=repo_context.strip() or "(no repository context available)",
    )


def extract_json(text: str) -> str:
    """Pull the JSON object out of a raw model response — same shape as the
    planner's extractor (bare object, fenced block, or first/last brace)."""
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return trimmed
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", trimmed)
    if fence and fence.group(1):
        return fence.group(1)
    first, last = trimmed.find("{"), trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    raise TriageError("No JSON object found in triage response", text)


def validate(parsed: object) -> TriageProposal:
    if not isinstance(parsed, dict):
        raise TriageError("triage output must be a JSON object")
    proposed_fix = str(parsed.get("proposed_fix", "")).strip()
    if not proposed_fix:
        # A proposal with no proposed fix is useless — treat as a triage failure
        # so the caller falls back to the raw ping rather than shipping an empty
        # "here's your fix:" that reads worse than the original alert.
        raise TriageError("triage output has no proposed_fix")
    confidence = str(parsed.get("confidence", "low")).strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"
    return TriageProposal(
        proposed_fix=proposed_fix,
        approve_hint=str(parsed.get("approve_hint", "")).strip(),
        dedupe_note=str(parsed.get("dedupe_note", "")).strip(),
        is_duplicate=bool(parsed.get("is_duplicate", False)),
        confidence=confidence,
    )


async def triage(
    problem: str, *, catalog: str, repo_context: str, caller: ClaudeCaller,
) -> "TriageProposal | None":
    """Run the triage step. Returns a validated :class:`TriageProposal`, or
    ``None`` on ANY failure (LLM error, invalid JSON, empty fix) — the caller
    then delivers the raw ping unchanged. Best-effort by contract: triage must
    never break the heartbeat or block a notification."""
    try:
        raw = await caller(build_prompt(problem, catalog, repo_context))
        parsed = json.loads(extract_json(raw))
        return validate(parsed)
    except Exception:  # noqa: BLE001 — fail toward the owner: fall back to the raw ping
        return None


def render(proposal: TriageProposal, raw_msg: str) -> str:
    """Build the enriched owner message from the raw ping + the proposal. Pure
    function (independently tested). Keeps the ORIGINAL alert verbatim at the
    top — triage enriches, it never replaces the ground-truth signal — then adds
    the dedupe note, the proposed fix, and how to approve it.

    The word "Proposed" is load-bearing: this is propose-only, the fix is NOT
    applied, and the owner must approve."""
    lines = [raw_msg.rstrip()]
    if proposal.dedupe_note:
        lines.append(f"\n🔁 {proposal.dedupe_note}")
    lines.append(f"\n💡 Proposed fix: {proposal.proposed_fix}")
    if proposal.approve_hint:
        lines.append(f"✅ To approve: {proposal.approve_hint}")
    lines.append("\n(proposed only — nothing has been changed; you decide)")
    return "\n".join(lines)


def format_catalog(problems: list[dict], *, limit: int = 20) -> str:
    """Render a :meth:`StateStore.list_problems` snapshot into the compact catalog
    block the prompt embeds. Best-effort, never raises — a bad row degrades to a
    skipped line, not a failed triage."""
    out: list[str] = []
    for p in (problems or [])[:limit]:
        try:
            cat = p.get("category", "?")
            kind = p.get("kind", "") or "?"
            summary = p.get("summary", "") or ""
            count = p.get("count", 0)
            term = p.get("terminal_count", 0)
            out.append(f"- [{cat}/{kind}] ×{count} (terminal {term}): {summary}")
        except Exception:  # noqa: BLE001 — a bad row must not sink the block
            continue
    return "\n".join(out)


def retention_context(size_bytes: int) -> str:
    """Grounded ``repo_context`` block for the DB-size alarm: the deterministic
    retention/alarm configuration + the current on-disk size, drawn from the
    same env helpers the alarm itself reads (:mod:`devclaw.state_store.core`).
    Best-effort, never raises — the section degrades gracefully.

    This is the #227 grounding discipline: the triage step reasons from these
    facts, not from anything it assumes about the host."""
    try:
        from ..state_store.core import (
            db_size_alert_bytes,
            events_retention_days,
            trace_retention_days,
        )

        size_mb = (size_bytes or 0) / (1024 * 1024)
        threshold_mb = db_size_alert_bytes() / (1024 * 1024) if db_size_alert_bytes() else 0
        trace_days = trace_retention_days()
        events_days = events_retention_days()
        return "\n".join(
            [
                f"current devclaw.db size (incl. WAL sidecar): {size_mb:.0f} MB",
                f"alarm threshold (DEVCLAW_DB_SIZE_ALERT_MB): "
                f"{threshold_mb:.0f} MB" if threshold_mb else
                "alarm threshold (DEVCLAW_DB_SIZE_ALERT_MB): disabled",
                f"trace retention (DEVCLAW_TRACE_RETENTION_DAYS): "
                + (f"{trace_days} days" if trace_days else "DISABLED (0) — traces are not pruned"),
                f"events retention (DEVCLAW_EVENTS_RETENTION_DAYS): "
                + (f"{events_days} days" if events_days else "DISABLED (0) — events are not pruned"),
                "note: retention + a weekly VACUUM keep the .db small; a retention "
                "var set to 0 disables that pruning, which is the usual root cause "
                "of unbounded growth.",
            ]
        )
    except Exception:  # noqa: BLE001 — grounding is best-effort, never fails the step
        return ""


def default_caller() -> ClaudeCaller:
    """The production cognition caller, bound to the triage tier. Imported lazily
    from devclaw's shared ``claude --print`` factory so unit tests (which inject
    a fake) never touch the subprocess."""
    from ..planner import claude_with_model

    return claude_with_model(TRIAGE_MODEL, role="triage")


#: enable flag for the whole interceptor. Default ON — the propose loop is the
#: point; ``0``/``false`` reverts every eligible ping to the raw path. Read as a
#: literal so the env-vars doc-sync test sees it.
def enabled() -> bool:
    return os.environ.get("DEVCLAW_SELF_TRIAGE", "1").strip().lower() not in ("0", "false", "")
