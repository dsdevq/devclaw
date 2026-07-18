"""Checklist parse/validate/round-trip + scheduling helpers."""

from __future__ import annotations

import pytest

from devclaw.goal.checklist import (
    ChecklistParseError,
    addresses_are_scaffold,
    dump_checklist,
    extract_yaml,
    parse_checklist,
    ready_items,
    update_item,
    validate_checklist,
)
from devclaw.goal.models import Checklist, ChecklistItem


# ---- happy-path parse ------------------------------------------------------


_GOOD = """\
checklist:
  - id: scaffold
    requirement: Create the FinanceSentry.Mcp.csproj.
    evidence_target: backend/src/FinanceSentry.Mcp/FinanceSentry.Mcp.csproj
    addresses_files: [backend/src/FinanceSentry.Mcp/FinanceSentry.Mcp.csproj]
    depends_on: []
    status: not_started
    evidence: null
  - id: wire-accounts
    requirement: Wire the accounts tool to GetAccountsQuery.
    evidence_target: backend/src/FinanceSentry.Mcp/Tools/AccountsTool.cs — Execute calls IQueryHandler<GetAccountsQuery,GetAccountsResult>
    addresses_files: [backend/src/FinanceSentry.Mcp/Tools/AccountsTool.cs]
    depends_on: [scaffold]
    status: not_started
    evidence: null
    effort_minutes: 15
    model_tier: sonnet
    note: GetAccountsQuery is in Modules.BankSync.
open_questions:
  - Are wealth-summary and transaction-summary the same tool?
notes:
  - The contract test items all touch the same file — serialize them.
"""


def test_parse_happy_path():
    cl = parse_checklist(_GOOD)
    assert [i.id for i in cl.items] == ["scaffold", "wire-accounts"]
    assert cl.items[1].depends_on == ["scaffold"]
    assert cl.items[1].effort_minutes == 15
    assert cl.items[1].model_tier == "sonnet"
    assert "Modules.BankSync" in cl.items[1].note
    assert cl.open_questions == ["Are wealth-summary and transaction-summary the same tool?"]
    assert cl.notes == ["The contract test items all touch the same file — serialize them."]


def test_attempts_round_trips_and_is_omitted_when_zero():
    # #6 circuit-breaker counter persists across dump→parse (so a mid-flight
    # attempt count survives a restart), but stays out of the YAML at the
    # default 0 to keep the round-trip byte-stable for the common case.
    parsed = parse_checklist(_GOOD)
    assert all(i.attempts == 0 for i in parsed.items)  # default when absent
    assert "attempts:" not in dump_checklist(parsed)  # omitted at 0

    bumped = Checklist(items=[
        ChecklistItem(**{**vars(parsed.items[0]), "attempts": 2}),
        parsed.items[1],
    ])
    reloaded = parse_checklist(dump_checklist(bumped))
    assert reloaded.items[0].attempts == 2
    assert reloaded.items[1].attempts == 0


# ---- preamble + fence tolerance (the model's first-pass output had both) ---


def test_extract_yaml_strips_leading_preamble():
    raw = (
        "The finance-sentry repo isn't mounted here — working purely from the digest.\n\n"
        + _GOOD
    )
    body = extract_yaml(raw)
    assert body.startswith("checklist:")


def test_extract_yaml_strips_markdown_fence():
    raw = "```yaml\n" + _GOOD + "```\n"
    body = extract_yaml(raw)
    assert body.startswith("checklist:")


def test_extract_yaml_rejects_empty():
    with pytest.raises(ChecklistParseError):
        extract_yaml("")


def test_extract_yaml_rejects_no_top_level_key():
    with pytest.raises(ChecklistParseError):
        extract_yaml("this is just prose with no yaml shape")


# ---- schema-failure paths --------------------------------------------------


def test_missing_checklist_key_rejected():
    with pytest.raises(ChecklistParseError):
        parse_checklist("notes:\n  - just a note\n")


def test_empty_checklist_rejected():
    with pytest.raises(ChecklistParseError):
        parse_checklist("checklist: []\n")


def test_items_missing_required_fields_are_dropped_not_fatal():
    raw = """\
checklist:
  - id: good
    requirement: A real requirement.
    evidence_target: somewhere/specific.py
  - id: bad-no-requirement
    evidence_target: foo.py
  - missing-id-and-requirement: yes
"""
    cl = parse_checklist(raw)
    assert [i.id for i in cl.items] == ["good"]


def test_all_items_invalid_raises():
    raw = """\
checklist:
  - id: bad
    requirement: missing evidence_target
  - id: alsobad
"""
    with pytest.raises(ChecklistParseError):
        parse_checklist(raw)


def test_duplicate_ids_keep_first():
    raw = """\
checklist:
  - id: dupe
    requirement: First.
    evidence_target: a.py
  - id: dupe
    requirement: Second.
    evidence_target: b.py
"""
    cl = parse_checklist(raw)
    assert len(cl.items) == 1
    assert cl.items[0].requirement == "First."


def test_dep_to_unknown_item_is_pruned():
    raw = """\
checklist:
  - id: a
    requirement: First.
    evidence_target: a.py
  - id: b
    requirement: Second.
    evidence_target: b.py
    depends_on: [a, ghost]
"""
    cl = parse_checklist(raw)
    assert cl.items[1].depends_on == ["a"]


def test_self_dep_dropped():
    raw = """\
checklist:
  - id: a
    requirement: ...
    evidence_target: a.py
    depends_on: [a]
"""
    cl = parse_checklist(raw)
    assert cl.items[0].depends_on == []


def test_invalid_status_falls_back_to_not_started():
    raw = """\
checklist:
  - id: a
    requirement: ...
    evidence_target: a.py
    status: vibes
"""
    cl = parse_checklist(raw)
    assert cl.items[0].status == "not_started"


def test_invalid_tier_dropped():
    raw = """\
checklist:
  - id: a
    requirement: ...
    evidence_target: a.py
    model_tier: gpt5
"""
    cl = parse_checklist(raw)
    assert cl.items[0].model_tier is None


def test_effort_minutes_accepts_string_int():
    raw = """\
checklist:
  - id: a
    requirement: ...
    evidence_target: a.py
    effort_minutes: "20"
"""
    cl = parse_checklist(raw)
    assert cl.items[0].effort_minutes == 20


def test_yaml_garbage_raises():
    with pytest.raises(ChecklistParseError):
        parse_checklist("checklist:\n  - id: [oops\n")


# ---- round-trip ------------------------------------------------------------


def test_dump_parse_round_trip_stable():
    cl = parse_checklist(_GOOD)
    serialized = dump_checklist(cl)
    cl2 = parse_checklist(serialized)
    assert cl == cl2


def test_dump_omits_none_optional_fields():
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
    ])
    out = dump_checklist(cl)
    assert "effort_minutes" not in out
    assert "model_tier" not in out
    # explicit empty per-item `note:` is dropped (different key from the
    # top-level `notes:` list which is always emitted).
    assert "  note:" not in out


# ---- ready_items ------------------------------------------------------------


def _cl(*items: ChecklistItem) -> Checklist:
    return Checklist(items=list(items))


def test_ready_items_no_deps_all_ready():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
        ChecklistItem(id="b", requirement="r", evidence_target="t"),
    )
    assert [i.id for i in ready_items(cl)] == ["a", "b"]


def test_ready_items_blocks_on_unmet_dep():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
        ChecklistItem(id="b", requirement="r", evidence_target="t", depends_on=["a"]),
    )
    # a is not_started → b is not ready (a isn't done yet)
    assert [i.id for i in ready_items(cl)] == ["a"]


def test_ready_items_unblocks_after_dep_done():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t", status="done", evidence="e"),
        ChecklistItem(id="b", requirement="r", evidence_target="t", depends_on=["a"]),
    )
    assert [i.id for i in ready_items(cl)] == ["b"]


def test_ready_items_excludes_in_flight_and_done():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t", status="in_flight"),
        ChecklistItem(id="b", requirement="r", evidence_target="t", status="done", evidence="e"),
        ChecklistItem(id="c", requirement="r", evidence_target="t"),
    )
    assert [i.id for i in ready_items(cl)] == ["c"]


def test_ready_items_deterministic_order_matches_checklist_order():
    cl = _cl(
        ChecklistItem(id="z", requirement="r", evidence_target="t"),
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
    )
    # Insertion order, not alphabetical — the scheduler depends on it.
    assert [i.id for i in ready_items(cl)] == ["z", "a"]


# ---- update_item ------------------------------------------------------------


def test_update_item_changes_status_and_evidence_immutably():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
    )
    cl2 = update_item(cl, "a", status="done", evidence="src/A.cs:12")
    assert cl is not cl2
    assert cl.items[0].status == "not_started"  # original untouched
    assert cl2.items[0].status == "done"
    assert cl2.items[0].evidence == "src/A.cs:12"


def test_update_item_unknown_id_raises():
    cl = _cl(ChecklistItem(id="a", requirement="r", evidence_target="t"))
    with pytest.raises(KeyError):
        update_item(cl, "ghost", status="done")


def test_update_item_preserves_other_items():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
        ChecklistItem(id="b", requirement="r", evidence_target="t"),
    )
    cl2 = update_item(cl, "b", status="in_flight")
    assert cl2.items[0] == cl.items[0]
    assert cl2.items[1].status == "in_flight"


# ---- milestone field --------------------------------------------------------
# Added 2026-06-30 — decomposer's spec input carries an `## Milestones` section
# but the checklist output flattened them away. milestone is now first-class.


_WITH_MILESTONES = """\
checklist:
  - id: scaffold-backend
    requirement: Scaffold the API project.
    evidence_target: backend/Crm.Api.csproj
    addresses_files: []
    depends_on: []
    status: not_started
    evidence: null
    milestone: "M1 — Skeleton"
  - id: contacts-crud
    requirement: Implement contacts CRUD endpoints.
    evidence_target: backend/Controllers/ContactsController.cs
    addresses_files: []
    depends_on: [scaffold-backend]
    status: not_started
    evidence: null
    milestone: "M2 — Contacts CRUD"
open_questions: []
notes: []
"""


def test_milestone_parses_when_present():
    cl = parse_checklist(_WITH_MILESTONES)
    assert cl.items[0].milestone == "M1 — Skeleton"
    assert cl.items[1].milestone == "M2 — Contacts CRUD"


def test_milestone_omitted_yields_none():
    """Items WITHOUT a milestone: key parse cleanly with milestone=None.
    Legacy decomposer output (pre-this-field) keeps working."""
    cl = parse_checklist(_GOOD)
    for item in cl.items:
        assert item.milestone is None


def test_milestone_non_string_dropped_to_none():
    """Defensive: a numeric or null milestone value is ignored, not propagated."""
    bad = _WITH_MILESTONES.replace('milestone: "M1 — Skeleton"', "milestone: 42")
    cl = parse_checklist(bad)
    assert cl.items[0].milestone is None


def test_milestone_blank_string_dropped_to_none():
    bad = _WITH_MILESTONES.replace('milestone: "M1 — Skeleton"', 'milestone: "   "')
    cl = parse_checklist(bad)
    assert cl.items[0].milestone is None


def test_dump_round_trip_preserves_milestone():
    cl = parse_checklist(_WITH_MILESTONES)
    cl2 = parse_checklist(dump_checklist(cl))
    assert cl == cl2


def test_dump_omits_milestone_when_none():
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
    ])
    out = dump_checklist(cl)
    assert "milestone" not in out


def test_update_item_preserves_milestone():
    """The settle hook flipping status/evidence must not blow away milestone."""
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t",
                      milestone="M1 — Skeleton"),
    )
    cl2 = update_item(cl, "a", status="done", evidence="src/A.cs:12")
    assert cl2.items[0].milestone == "M1 — Skeleton"


# ---- scaffold field (L3, #222) ---------------------------------------------
# A generated-scaffolding item (`ng new` / `dotnet new` output) is tagged so the
# dispatch path can skip the adversarial review gate — verified structurally
# instead. The parser must read it; round-trip must preserve it; the derivation
# helper must be conservative (never drag a real item out of review).


_WITH_SCAFFOLD = """\
checklist:
  - id: scaffold-workspace
    requirement: Run `ng new crm-web` and commit the generated workspace.
    evidence_target: frontend/crm-web/angular.json
    addresses_files: [frontend/crm-web/angular.json]
    depends_on: []
    status: not_started
    evidence: null
    scaffold: true
  - id: contacts-list
    requirement: Implement the contacts list component.
    evidence_target: frontend/crm-web/src/app/contacts/contacts.component.ts
    addresses_files: [frontend/crm-web/src/app/contacts/contacts.component.ts]
    depends_on: [scaffold-workspace]
    status: not_started
    evidence: null
open_questions: []
notes: []
"""


def test_scaffold_parses_when_true():
    cl = parse_checklist(_WITH_SCAFFOLD)
    assert cl.items[0].scaffold is True
    # a normal implementation item stays non-scaffold
    assert cl.items[1].scaffold is False


def test_scaffold_omitted_yields_false():
    cl = parse_checklist(_GOOD)
    for item in cl.items:
        assert item.scaffold is False


def test_scaffold_accepts_string_true():
    raw = _WITH_SCAFFOLD.replace("scaffold: true", 'scaffold: "yes"')
    cl = parse_checklist(raw)
    assert cl.items[0].scaffold is True


def test_scaffold_junk_value_is_false():
    """A non-boolean, non-truthy value defaults to False — the conservative
    read (an item is scaffold ONLY when the decomposer clearly says so)."""
    raw = _WITH_SCAFFOLD.replace("scaffold: true", "scaffold: maybe")
    cl = parse_checklist(raw)
    assert cl.items[0].scaffold is False


def test_dump_round_trip_preserves_scaffold():
    cl = parse_checklist(_WITH_SCAFFOLD)
    cl2 = parse_checklist(dump_checklist(cl))
    assert cl == cl2
    assert cl2.items[0].scaffold is True


def test_dump_omits_scaffold_when_false():
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
    ])
    out = dump_checklist(cl)
    assert "scaffold" not in out


def test_update_item_preserves_scaffold():
    """The settle hook flipping status/evidence must not blow away scaffold."""
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t", scaffold=True),
    )
    cl2 = update_item(cl, "a", status="done", evidence="src/A.cs:12")
    assert cl2.items[0].scaffold is True


# ---- addresses_are_scaffold (the dispatch-time derivation) ------------------


def _scaffold_cl() -> Checklist:
    return _cl(
        ChecklistItem(id="scaf", requirement="r", evidence_target="t", scaffold=True),
        ChecklistItem(id="scaf2", requirement="r", evidence_target="t", scaffold=True),
        ChecklistItem(id="logic", requirement="r", evidence_target="t"),
    )


def test_addresses_are_scaffold_true_when_all_scaffold():
    cl = _scaffold_cl()
    assert addresses_are_scaffold(cl, ["scaf"]) is True
    assert addresses_are_scaffold(cl, ["scaf", "scaf2"]) is True


def test_addresses_are_scaffold_false_for_logic_item():
    cl = _scaffold_cl()
    assert addresses_are_scaffold(cl, ["logic"]) is False


def test_addresses_are_scaffold_false_for_mixed_action():
    """A mixed action (a scaffold item AND a real code item) is NOT scaffold —
    it must go through review. This is the over-tag guard: one scaffold item
    can't drag a logic item out of the review gate."""
    cl = _scaffold_cl()
    assert addresses_are_scaffold(cl, ["scaf", "logic"]) is False


def test_addresses_are_scaffold_false_when_no_checklist_or_no_addresses():
    cl = _scaffold_cl()
    assert addresses_are_scaffold(None, ["scaf"]) is False
    assert addresses_are_scaffold(cl, []) is False


def test_addresses_are_scaffold_false_when_ids_unknown():
    """An action citing only unknown ids resolves to zero items → not scaffold
    (fail-safe: never skip review on an unresolvable reference)."""
    cl = _scaffold_cl()
    assert addresses_are_scaffold(cl, ["ghost"]) is False
