"""Schema round-trip + parse-validation for ``firmed-draft.yaml``.

The contract: a well-formed firming response parses cleanly; the post-parse
cross-check forces ``status: firmed`` only when ``unknowns`` is empty (so a
model claiming firmed-with-unknowns is forced back to needs_owner_answers);
preamble + ```yaml``` fences are tolerated; round-trips through dump_firmed."""

from __future__ import annotations

import pytest

from devclaw.goal.firmed import (
    FirmedGoal,
    FirmedParseError,
    SuccessCriterion,
    Unknown,
    dump_firmed,
    parse_firmed,
)


GOOD_YAML = """\
status: needs_owner_answers
round: 1
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates Transaction rows by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
conventions_to_follow:
  - CQRS via IQueryHandler<TQuery, TResult>
unknowns:
  - id: cf-u1
    question: Period model — calendar month, rolling 30d, or configurable?
    why: No existing reporting framework in repo to copy from.
    options: [calendar_month, rolling_30d, configurable]
    default_if_no_answer: calendar_month
blockers:
  - no shared aggregation utility — must build BudgetSpendingCalculator-style helper
stub_acceptable: []
descoped:
  - no per-day granularity in v1
"""


def test_parse_well_formed_round_1():
    firmed = parse_firmed(GOOD_YAML)
    assert firmed.status == "needs_owner_answers"
    assert firmed.round == 1
    assert firmed.intent == "build the cashflow report"
    assert len(firmed.success_criteria) == 1
    assert firmed.success_criteria[0] == SuccessCriterion(
        id="cf-1",
        text="report aggregates Transaction rows by calendar month",
        verifiable_by="CashflowReportTests.GroupsByMonth",
    )
    assert firmed.conventions_to_follow == ["CQRS via IQueryHandler<TQuery, TResult>"]
    assert len(firmed.unknowns) == 1
    u = firmed.unknowns[0]
    assert u.id == "cf-u1"
    assert u.options == ["calendar_month", "rolling_30d", "configurable"]
    assert u.default_if_no_answer == "calendar_month"
    assert firmed.descoped == ["no per-day granularity in v1"]


def test_round_trip_through_dump():
    firmed = parse_firmed(GOOD_YAML)
    rendered = dump_firmed(firmed)
    parsed_again = parse_firmed(rendered)
    assert parsed_again == firmed


def test_status_firmed_with_lingering_unknowns_is_forced_back():
    """The post-parse contract: status=firmed iff unknowns is empty. A model
    that emits status: firmed with unknowns still present is forced back to
    needs_owner_answers — the disk is the truth, not what the model claims."""
    raw = (
        "status: firmed\n"
        "round: 2\n"
        "intent: build the cashflow report\n"
        "unknowns:\n"
        "  - id: cf-u2\n"
        "    question: monthly or quarterly?\n"
    )
    firmed = parse_firmed(raw)
    assert firmed.status == "needs_owner_answers"


def test_verify_cmd_round_trip_when_set():
    """When firming derives a new verify_cmd (e.g. cf-11 = gate must run
    playwright), the field round-trips cleanly so load_effective_goal can
    overlay it. Closes the cf-11 churn root cause."""
    raw = (
        "status: firmed\n"
        "round: 2\n"
        "intent: build it\n"
        "success_criteria:\n"
        "  - id: c1\n"
        "    text: pytest + playwright both run\n"
        "    verifiable_by: gate exit 0\n"
        "verify_cmd: pytest -q && npx playwright test --reporter=list\n"
    )
    firmed = parse_firmed(raw)
    assert firmed.verify_cmd == "pytest -q && npx playwright test --reporter=list"
    rendered = dump_firmed(firmed)
    parsed_again = parse_firmed(rendered)
    assert parsed_again.verify_cmd == firmed.verify_cmd


def test_verify_cmd_defaults_to_none_when_omitted():
    """When firming does not specify a verify_cmd (the existing one already
    covers the criteria), the field is None and load_effective_goal preserves
    the base goal's verify_cmd."""
    raw = (
        "status: firmed\n"
        "round: 1\n"
        "intent: build it\n"
        "success_criteria:\n"
        "  - id: c1\n"
        "    text: a thing\n"
        "    verifiable_by: tests pass\n"
    )
    firmed = parse_firmed(raw)
    assert firmed.verify_cmd is None


def test_verify_cmd_empty_string_normalized_to_none():
    """Defensive: a model that emits `verify_cmd: ''` or whitespace should be
    treated the same as omitting the field — no spurious overlay."""
    raw = (
        "status: firmed\n"
        "round: 1\n"
        "intent: build it\n"
        "success_criteria:\n"
        "  - id: c1\n"
        "    text: thing\n"
        "    verifiable_by: tests\n"
        "verify_cmd: '   '\n"
    )
    firmed = parse_firmed(raw)
    assert firmed.verify_cmd is None


def test_tolerates_preamble_and_fence():
    """Models occasionally wrap output or precede with prose; both are stripped."""
    wrapped = "Sure, here's the firmed draft:\n\n```yaml\n" + GOOD_YAML + "\n```"
    parsed = parse_firmed(wrapped)
    assert parsed.intent == "build the cashflow report"


def test_empty_input_raises():
    with pytest.raises(FirmedParseError):
        parse_firmed("")


def test_missing_status_raises():
    with pytest.raises(FirmedParseError):
        parse_firmed("intent: x\nround: 1\n")


def test_missing_intent_raises():
    with pytest.raises(FirmedParseError):
        parse_firmed("status: firmed\nround: 1\n")


def test_invalid_status_raises():
    with pytest.raises(FirmedParseError):
        parse_firmed("status: half_baked\nintent: x\nround: 1\n")


def test_firmed_clean_round_passes_through():
    raw = (
        "status: firmed\n"
        "round: 2\n"
        "intent: build the cashflow report\n"
        "success_criteria:\n"
        "  - id: cf-1\n"
        "    text: aggregates by calendar month\n"
        "    verifiable_by: CashflowReportTests.GroupsByMonth\n"
        "unknowns: []\n"
    )
    firmed = parse_firmed(raw)
    assert firmed.status == "firmed"
    assert firmed.round == 2
    assert firmed.unknowns == []
