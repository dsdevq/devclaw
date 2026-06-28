"""The firmed-goal artifact — structured representation of what firming produces.

A :class:`FirmedGoal` is what the firming phase commits to disk as
``firmed-draft.yaml`` (and, once the owner has answered every ``unknown``, the
same file with ``status: firmed``). The decomposer reads it instead of the raw
``goal.yaml``: success criteria already decomposed into clauses with stable ids,
conventions extracted from research, blockers named, ``stub_acceptable``
populated by owner intent rather than fabrication.

Schema mirrors ``devclaw/prompts/firming.md`` — change one, change the other.
Pure dataclasses + YAML parse/dump; no I/O, no cognition.

One-file design (proposal Q1, current lean): a single ``firmed-draft.yaml``
with a ``status`` field replaces the draft-vs-snapshot pair. The decomposer
refuses to read it unless ``status == firmed``. Git history is the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import yaml

FirmedStatus = Literal["needs_owner_answers", "firmed"]

_FENCE = re.compile(r"```(?:yaml|yml)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)


@dataclass(frozen=True)
class SuccessCriterion:
    """One atomic completion clause, pre-decomposed by firming so the
    evaluator + executor have a stable id to refer to. Replaces the
    free-form ``done_when`` prose for firmed goals."""

    id: str
    text: str
    verifiable_by: str = ""


@dataclass(frozen=True)
class Unknown:
    """A question firming couldn't answer from research. The owner answers
    these via the ``answer_unknowns`` MCP tool. ``options`` is the chooser
    surface the waiter renders; ``default_if_no_answer`` is documentation
    only in v1 (NOT auto-fired — see proposal Q2)."""

    id: str
    question: str
    why: str = ""
    options: list[str] = field(default_factory=list)
    default_if_no_answer: str | None = None


@dataclass(frozen=True)
class FirmedGoal:
    """The full firming output. ``status == firmed`` means decomposer may
    read it; ``needs_owner_answers`` means the goal is parked at
    ``phase=blocked`` waiting on answers.

    ``verify_cmd``: when firming derives a stricter or different gate from
    the success criteria (e.g. cf-11 = "gate runs pytest AND playwright"),
    the model outputs the full replacement command here. ``None`` means the
    original ``goal.yaml`` verify_cmd is correct as-is. ``load_effective_goal``
    overlays this so the done-gate, the planner, and the executor all see
    the firmed gate — the cascade can never silently disagree with itself.
    Closes the cf-11 churn root cause: PRs invented Makefile/pytest-wrapper
    hacks to smuggle Playwright through a stale verify_cmd because firming
    couldn't update the goal's gate.
    """

    status: FirmedStatus
    round: int
    intent: str
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    conventions_to_follow: list[str] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    stub_acceptable: list[str] = field(default_factory=list)
    descoped: list[str] = field(default_factory=list)
    verify_cmd: str | None = None


class FirmedParseError(Exception):
    """Raised when the firming model emits unparseable YAML or violates the
    schema (e.g. ``status`` missing). Carries the raw text on ``.raw`` so the
    caller can log it for prompt-iteration."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def extract_yaml(text: str) -> str:
    """Pull the YAML body out of the firming response. Tolerates a wrapping
    ``yaml`` markdown fence and a one-line conversational preamble before the
    first ``status:`` key."""
    trimmed = (text or "").strip()
    if not trimmed:
        raise FirmedParseError("firming returned empty output", text)
    fence_match = _FENCE.search(trimmed)
    if fence_match:
        return fence_match.group(1).strip()
    idx = trimmed.find("status:")
    if idx >= 0:
        return trimmed[idx:].strip()
    raise FirmedParseError(
        "firming output has no `status:` top-level key — model ignored the schema",
        text,
    )


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        s = str(entry).strip()
        if s:
            out.append(s)
    return out


def _parse_criterion(raw: object) -> SuccessCriterion | None:
    if not isinstance(raw, dict):
        return None
    id_ = str(raw.get("id", "")).strip()
    text = str(raw.get("text", "")).strip()
    if not id_ or not text:
        return None
    return SuccessCriterion(
        id=id_, text=text,
        verifiable_by=str(raw.get("verifiable_by", "")).strip(),
    )


def _parse_unknown(raw: object) -> Unknown | None:
    if not isinstance(raw, dict):
        return None
    id_ = str(raw.get("id", "")).strip()
    question = str(raw.get("question", "")).strip()
    if not id_ or not question:
        return None
    default_raw = raw.get("default_if_no_answer")
    default = str(default_raw).strip() if default_raw is not None and str(default_raw).strip() else None
    return Unknown(
        id=id_, question=question,
        why=str(raw.get("why", "")).strip(),
        options=_str_list(raw.get("options")),
        default_if_no_answer=default,
    )


def parse_firmed(text: str) -> FirmedGoal:
    """Parse raw firming output into a :class:`FirmedGoal`. Raises
    :class:`FirmedParseError` on YAML/schema failure."""
    body = extract_yaml(text)
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise FirmedParseError(f"firming YAML invalid: {exc}", text) from exc
    if not isinstance(data, dict):
        raise FirmedParseError("firming root must be a mapping", text)

    status_raw = str(data.get("status", "")).strip()
    if status_raw not in ("needs_owner_answers", "firmed"):
        raise FirmedParseError(
            f"firming.status must be 'needs_owner_answers' or 'firmed', got {status_raw!r}",
            text,
        )

    intent = str(data.get("intent", "")).strip()
    if not intent:
        raise FirmedParseError("firming.intent is required", text)

    try:
        round_ = int(data.get("round", 1))
    except (TypeError, ValueError):
        round_ = 1

    criteria_raw = data.get("success_criteria") or []
    criteria = [c for c in (_parse_criterion(x) for x in criteria_raw) if c is not None]

    unknowns_raw = data.get("unknowns") or []
    unknowns = [u for u in (_parse_unknown(x) for x in unknowns_raw) if u is not None]

    # Cross-field sanity: status=firmed iff unknowns is empty. A model that
    # claims firmed but left unknowns lying around is forced back into
    # needs_owner_answers — the contract is what the disk says, not what the
    # model claims.
    status: FirmedStatus = "firmed" if (status_raw == "firmed" and not unknowns) else "needs_owner_answers"

    verify_cmd_raw = data.get("verify_cmd")
    verify_cmd = str(verify_cmd_raw).strip() if verify_cmd_raw is not None and str(verify_cmd_raw).strip() else None

    return FirmedGoal(
        status=status,
        round=round_,
        intent=intent,
        success_criteria=criteria,
        conventions_to_follow=_str_list(data.get("conventions_to_follow")),
        unknowns=unknowns,
        blockers=_str_list(data.get("blockers")),
        stub_acceptable=_str_list(data.get("stub_acceptable")),
        descoped=_str_list(data.get("descoped")),
        verify_cmd=verify_cmd,
    )


def derive_done_when(firmed: FirmedGoal) -> str:
    """Synthesize a ``done_when``-style AND-joined prose string from a firmed
    goal's success criteria. Used by the decomposer + done-gate evaluator so
    the (now structured) success_criteria look like the legacy free-form
    done_when string those layers already accept. Returns the empty string
    when no criteria are present — caller falls back to the original
    ``goal.done_when``."""
    texts = [c.text.strip() for c in firmed.success_criteria if c.text and c.text.strip()]
    return " AND ".join(texts)


def dump_firmed(firmed: FirmedGoal) -> str:
    """Serialize back to YAML — round-trips cleanly through parse_firmed."""
    data = {
        "status": firmed.status,
        "round": firmed.round,
        "intent": firmed.intent,
        "success_criteria": [
            {"id": c.id, "text": c.text, "verifiable_by": c.verifiable_by}
            for c in firmed.success_criteria
        ],
        "conventions_to_follow": list(firmed.conventions_to_follow),
        "unknowns": [
            {
                "id": u.id, "question": u.question, "why": u.why,
                "options": list(u.options),
                "default_if_no_answer": u.default_if_no_answer,
            }
            for u in firmed.unknowns
        ],
        "blockers": list(firmed.blockers),
        "stub_acceptable": list(firmed.stub_acceptable),
        "descoped": list(firmed.descoped),
        "verify_cmd": firmed.verify_cmd,
    }
    return yaml.safe_dump(data, sort_keys=False)
