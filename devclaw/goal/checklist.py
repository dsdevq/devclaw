"""Parse + validate the decomposer's YAML output into a :class:`Checklist`.

Pure functions, no I/O. The decomposer module owns the cognition call; this
module owns the schema contract: missing required fields make the item
invalid, extra top-level keys are dropped, defaults applied where the
decomposer left a field off.

The schema mirrors ``devclaw/prompts/decomposer.md`` — if you change one,
change the other.
"""

from __future__ import annotations

import re

import yaml

from .models import Checklist, ChecklistItem, ItemModelTier, ItemStatus

_VALID_STATUSES: tuple[ItemStatus, ...] = (
    "not_started",
    "in_flight",
    "done",
    "blocked",
    "mis_specified",
)
_VALID_TIERS: tuple[ItemModelTier, ...] = ("haiku", "sonnet", "opus")

# Decomposer prompt asks for raw YAML beginning with ``checklist:`` — but
# models still occasionally wrap in a markdown ```yaml``` fence or precede
# the YAML with a one-line preamble. Strip both so the parser never wedges
# on cosmetic noise.
_FENCE = re.compile(r"```(?:yaml|yml)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)


class ChecklistParseError(Exception):
    """The decomposer's raw output couldn't be parsed/validated. Carries the
    raw text on ``.raw`` so the caller can log it for prompt-iteration."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def extract_yaml(text: str) -> str:
    """Pull the YAML body out of the decomposer's response. Tolerates:
    (a) a leading one-line conversational preamble before ``checklist:``;
    (b) a wrapping ```yaml``` markdown fence; (c) raw YAML (the happy path)."""
    trimmed = (text or "").strip()
    if not trimmed:
        raise ChecklistParseError("decomposer returned empty output", text)
    fence_match = _FENCE.search(trimmed)
    if fence_match:
        return fence_match.group(1).strip()
    # Find the first occurrence of a top-level key (checklist: at column 0).
    # Anything before it is preamble noise.
    for marker in ("checklist:", "open_questions:", "notes:"):
        idx = trimmed.find(marker)
        if idx >= 0:
            return trimmed[idx:].strip()
    raise ChecklistParseError(
        "decomposer output has no `checklist:` / `open_questions:` / `notes:` "
        "top-level key — model probably ignored the schema",
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


#: newest failure notes kept per item — enough to rule out repeated
#: approaches without growing the brief unboundedly (ITEM_MAX_ATTEMPTS is 3,
#: so 5 covers the breaker window even after a manual unblock + re-run).
FAILURE_LOG_KEEP = 5


def _parse_item(raw: object) -> ChecklistItem | None:
    """Validate one item. Returns ``None`` (caller filters) if a required
    field is missing or malformed — better to drop a bad item than reject the
    whole checklist; the planner reading the checklist sees what survived,
    and the open_questions log can carry the explanation."""
    if not isinstance(raw, dict):
        return None
    id_ = str(raw.get("id", "")).strip()
    requirement = str(raw.get("requirement", "")).strip()
    evidence_target = str(raw.get("evidence_target", "")).strip()
    if not id_ or not requirement or not evidence_target:
        return None

    status_raw = str(raw.get("status", "not_started")).strip()
    status: ItemStatus = status_raw if status_raw in _VALID_STATUSES else "not_started"  # type: ignore[assignment]

    tier_raw = raw.get("model_tier")
    tier: ItemModelTier | None = None
    if isinstance(tier_raw, str) and tier_raw.strip() in _VALID_TIERS:
        tier = tier_raw.strip()  # type: ignore[assignment]

    effort_raw = raw.get("effort_minutes")
    effort: int | None = None
    if isinstance(effort_raw, int) and effort_raw > 0:
        effort = effort_raw
    elif isinstance(effort_raw, str) and effort_raw.strip().isdigit():
        n = int(effort_raw.strip())
        if n > 0:
            effort = n

    evidence_raw = raw.get("evidence")
    evidence: str | None = None
    if isinstance(evidence_raw, str) and evidence_raw.strip():
        evidence = evidence_raw.strip()

    milestone_raw = raw.get("milestone")
    milestone: str | None = None
    if isinstance(milestone_raw, str) and milestone_raw.strip():
        milestone = milestone_raw.strip()

    attempts_raw = raw.get("attempts")
    attempts = 0
    if isinstance(attempts_raw, bool):
        attempts = 0  # a stray YAML bool is not a count
    elif isinstance(attempts_raw, int) and attempts_raw > 0:
        attempts = attempts_raw
    elif isinstance(attempts_raw, str) and attempts_raw.strip().isdigit():
        attempts = int(attempts_raw.strip())

    failure_log = _str_list(raw.get("failure_log"))[-FAILURE_LOG_KEEP:]

    return ChecklistItem(
        id=id_,
        requirement=requirement,
        evidence_target=evidence_target,
        addresses_files=_str_list(raw.get("addresses_files")),
        depends_on=_str_list(raw.get("depends_on")),
        status=status,
        evidence=evidence,
        effort_minutes=effort,
        model_tier=tier,
        note=str(raw.get("note", "")).strip(),
        milestone=milestone,
        scaffold=_parse_bool(raw.get("scaffold")),
        attempts=attempts,
        failure_log=failure_log,
    )


def _parse_bool(raw: object) -> bool:
    """Tolerant truthy read for a YAML boolean field. Accepts a real bool,
    or the strings ``true``/``yes``/``1`` (case-insensitive). Anything else —
    including a missing key — is False (the conservative default: an item is
    only scaffold when the decomposer explicitly says so)."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1")
    return False


def _dedup_keep_first(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def validate_checklist(parsed: object) -> Checklist:
    """Top-level validation. Drops malformed items, validates dependency
    closure (every ``depends_on`` id refers to a real item in the same
    checklist; orphans are silently dropped), and rejects items with a
    self-cycle."""
    if not isinstance(parsed, dict):
        raise ChecklistParseError("checklist root must be a YAML mapping")

    raw_items = parsed.get("checklist")
    if not isinstance(raw_items, list) or not raw_items:
        raise ChecklistParseError(
            "checklist must be a non-empty list under the `checklist:` key"
        )

    items: list[ChecklistItem] = []
    seen_ids: set[str] = set()
    for raw_item in raw_items:
        item = _parse_item(raw_item)
        if item is None:
            continue
        if item.id in seen_ids:
            continue  # duplicate id — keep the first occurrence
        seen_ids.add(item.id)
        items.append(item)

    if not items:
        raise ChecklistParseError("no valid items in checklist after validation")

    # Prune depends_on that point at unknown items, drop self-deps, dedup.
    cleaned: list[ChecklistItem] = []
    for item in items:
        deps = [
            d for d in _dedup_keep_first(item.depends_on)
            if d in seen_ids and d != item.id
        ]
        cleaned.append(
            ChecklistItem(
                id=item.id,
                requirement=item.requirement,
                evidence_target=item.evidence_target,
                addresses_files=_dedup_keep_first(item.addresses_files),
                depends_on=deps,
                status=item.status,
                evidence=item.evidence,
                effort_minutes=item.effort_minutes,
                model_tier=item.model_tier,
                note=item.note,
                milestone=item.milestone,
                scaffold=item.scaffold,
                attempts=item.attempts,
                failure_log=list(item.failure_log),
            )
        )

    return Checklist(
        items=cleaned,
        open_questions=_str_list(parsed.get("open_questions")),
        notes=_str_list(parsed.get("notes")),
    )


def parse_checklist(text: str) -> Checklist:
    """Top-level entry point: raw decomposer text → validated Checklist.
    Raises :class:`ChecklistParseError` on YAML errors or schema failures."""
    yaml_body = extract_yaml(text)
    try:
        parsed = yaml.safe_load(yaml_body)
    except yaml.YAMLError as exc:
        raise ChecklistParseError(f"YAML parse failed: {exc}", text) from exc
    return validate_checklist(parsed)


def dump_checklist(checklist: Checklist) -> str:
    """Serialize a :class:`Checklist` back to YAML for on-disk persistence.
    Round-trip stable with :func:`parse_checklist` — the same checklist
    reloads to an equal object."""

    def _item_dict(item: ChecklistItem) -> dict[str, object]:
        d: dict[str, object] = {
            "id": item.id,
            "requirement": item.requirement,
            "evidence_target": item.evidence_target,
            "addresses_files": list(item.addresses_files),
            "depends_on": list(item.depends_on),
            "status": item.status,
            "evidence": item.evidence,
        }
        if item.effort_minutes is not None:
            d["effort_minutes"] = item.effort_minutes
        if item.model_tier is not None:
            d["model_tier"] = item.model_tier
        if item.note:
            d["note"] = item.note
        if item.milestone:
            d["milestone"] = item.milestone
        if item.scaffold:
            d["scaffold"] = True
        if item.attempts:
            d["attempts"] = item.attempts
        if item.failure_log:
            d["failure_log"] = list(item.failure_log)
        return d

    payload: dict[str, object] = {
        "checklist": [_item_dict(i) for i in checklist.items],
        "open_questions": list(checklist.open_questions),
        "notes": list(checklist.notes),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def ready_items(checklist: Checklist) -> list[ChecklistItem]:
    """Items the scheduler may dispatch this tick: status=='not_started' AND
    every dep resolves to a checklist item with status=='done'. Returned in
    checklist order so deterministic under test."""
    done_ids = {i.id for i in checklist.items if i.status == "done"}
    out: list[ChecklistItem] = []
    for item in checklist.items:
        if item.status != "not_started":
            continue
        if all(dep in done_ids for dep in item.depends_on):
            out.append(item)
    return out


def update_item(
    checklist: Checklist,
    item_id: str,
    *,
    status: ItemStatus | None = None,
    evidence: str | None = None,
    attempts: int | None = None,
    failure_note: str | None = None,
    clear_failure_log: bool = False,
) -> Checklist:
    """Return a new Checklist with the named item updated. Pure — does not
    mutate the input. Used by the runner's settle hook (status=done +
    evidence + ``clear_failure_log``), the scheduler (status=in_flight at
    dispatch), and the settle hook's per-item circuit breaker (``attempts``
    bump + ``failure_note`` append on a failed settle). ``failure_note``
    APPENDS to the item's bounded ``failure_log`` (newest
    :data:`FAILURE_LOG_KEEP` kept); ``clear_failure_log`` empties it — a
    proven item carries no stale failure history."""
    updated: list[ChecklistItem] = []
    found = False
    for item in checklist.items:
        if item.id != item_id:
            updated.append(item)
            continue
        found = True
        if clear_failure_log:
            new_log: list[str] = []
        elif failure_note is not None and failure_note.strip():
            new_log = (list(item.failure_log) + [failure_note.strip()])[-FAILURE_LOG_KEEP:]
        else:
            new_log = list(item.failure_log)
        updated.append(
            ChecklistItem(
                id=item.id,
                requirement=item.requirement,
                evidence_target=item.evidence_target,
                addresses_files=item.addresses_files,
                depends_on=item.depends_on,
                status=status if status is not None else item.status,
                evidence=evidence if evidence is not None else item.evidence,
                effort_minutes=item.effort_minutes,
                model_tier=item.model_tier,
                note=item.note,
                milestone=item.milestone,
                scaffold=item.scaffold,
                attempts=attempts if attempts is not None else item.attempts,
                failure_log=new_log,
            )
        )
    if not found:
        raise KeyError(f"no checklist item with id {item_id!r}")
    return Checklist(items=updated, open_questions=checklist.open_questions, notes=checklist.notes)


def addresses_are_scaffold(
    checklist: Checklist | None, addresses: list[str]
) -> bool:
    """True iff an action addressing ``addresses`` is a pure-scaffolding
    dispatch — used at dispatch to decide whether the resulting task skips the
    adversarial review gate (L3, #222). Deliberately CONSERVATIVE, so an
    over-tagged item can't drag a real code item out of review:

    - no checklist / no addresses → False (legacy backlog mode, or a free-form
      action the decomposer never tagged);
    - True only when the addressed ids resolve to ≥1 real checklist item AND
      EVERY resolved item has ``scaffold=True``. A mixed action (one scaffold +
      one logic item) is NOT scaffold — it goes through review.

    Mechanism, not cognition: the scaffold decision was made ONCE by the
    decomposer; this just propagates it. The per-tick planner never sets it."""
    if checklist is None or not addresses:
        return False
    by_id = {i.id: i for i in checklist.items}
    resolved = [by_id[a] for a in addresses if a in by_id]
    return bool(resolved) and all(i.scaffold for i in resolved)
