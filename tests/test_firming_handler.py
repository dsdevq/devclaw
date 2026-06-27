"""Unit tests for ``FirmingHandler`` — the first PhaseHandler.

Two state-machine paths matter:

  * round 1 → all-answered draft → lifecycle transitions to ``executing``,
    ADVANCED outcome
  * round 1 → draft with unknowns → ``phase=blocked``, BLOCKED outcome, owner
    ping sent

Plus round-N via ``handle_answer`` (the MCP entry point): merge prior + answers,
re-firm, same two terminal states. The cognition caller is faked — these are
state-machine tests, not LLM tests."""

from __future__ import annotations

import pytest

from devclaw.goal.firmed import parse_firmed
from devclaw.goal.models import GoalStatus
from devclaw.goal.phases import PhaseResult
from devclaw.goal.phases.firming import FirmingHandler
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import TickContext
from tests.goal_fakes import (
    Clock,
    FakeClaude,
    FakeEngine,
    RecordingNotifier,
    fake_prepare,
    seed_goal,
)


DRAFT_WITH_UNKNOWNS = """\
status: needs_owner_answers
round: 1
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates Transaction rows by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns:
  - id: cf-u1
    question: Period model — calendar month or rolling 30d?
    why: No existing reporting framework to copy from.
    options: [calendar_month, rolling_30d]
"""

DRAFT_FIRMED = """\
status: firmed
round: 2
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates Transaction rows by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns: []
"""

# A minimal valid checklist YAML for the decomposer fake — exercises the real
# parse path so we know the firming → decomposer wiring carries through.
DECOMPOSER_CHECKLIST_YAML = """\
checklist:
  - id: cf-impl
    requirement: implement CashflowReportService groups Transaction by month
    evidence_target: backend/src/CashflowReportService.cs:GetMonthly
    status: not_started
  - id: cf-tests
    requirement: tests assert monthly grouping shape
    evidence_target: backend/tests/CashflowReportTests.cs:GroupsByMonth
    depends_on: [cf-impl]
    status: not_started
"""


def _ctx(store: GoalStore, notifier: RecordingNotifier) -> TickContext:
    return TickContext(
        store=store,
        engine=FakeEngine(),
        planner_caller=FakeClaude(),
        evaluator_caller=FakeClaude(),
        notifier=notifier,
        prepare_ws=fake_prepare,
    )


@pytest.fixture
def store_with_goal(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="firming"))
    return store


@pytest.mark.asyncio
async def test_round_1_with_unknowns_blocks_and_pings_owner(store_with_goal):
    """Firming round 1 surfaced unknowns → goal parks at phase=blocked, an
    OWNER-level Telegram doorbell goes out, the draft is persisted, lifecycle
    stays at firming (the owner answers via answer_unknowns)."""
    store = store_with_goal
    notifier = RecordingNotifier()
    caller = FakeClaude(response=DRAFT_WITH_UNKNOWNS, role="goal_firming")
    handler = FirmingHandler(caller=caller)
    goal = store.load_goal("g")
    status = store.load_status("g")

    result = await handler.run("g", goal, status, _ctx(store, notifier))

    assert isinstance(result, PhaseResult)
    assert result.outcome == "blocked"
    assert caller.calls == 1
    after = store.load_status("g")
    assert after.lifecycle == "firming"
    assert after.phase == "blocked"
    assert "1 question" in (after.blocked_on or "")
    # draft persisted with the unknown the owner needs to answer
    draft = store.read_firmed_draft("g")
    assert draft is not None
    assert [u.id for u in draft.unknowns] == ["cf-u1"]
    # owner ping went out
    assert any("needs you" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_round_1_clean_firmed_advances_to_executing(store_with_goal):
    """No unknowns → lifecycle flips straight to executing, phase=idle, AND
    the decomposer fires inline so the checklist is on disk before the next
    tick (closes the silent-regression-to-backlog gap)."""
    store = store_with_goal
    notifier = RecordingNotifier()
    firming = FakeClaude(response=DRAFT_FIRMED, role="goal_firming")
    decomposer = FakeClaude(response=DECOMPOSER_CHECKLIST_YAML, role="goal_decomposer")
    handler = FirmingHandler(caller=firming, decomposer_caller=decomposer)
    goal = store.load_goal("g")
    status = store.load_status("g")

    result = await handler.run("g", goal, status, _ctx(store, notifier))

    assert result.outcome == "advanced"
    after = store.load_status("g")
    assert after.lifecycle == "executing"
    assert after.phase == "idle"
    draft = store.read_firmed_draft("g")
    assert draft is not None and draft.status == "firmed"
    # decomposer fired with the SYNTHESIZED done_when from success_criteria
    assert decomposer.calls == 1
    assert "calendar month" in decomposer.last_prompt
    # checklist now persisted — executor will pick it up
    checklist = store.read_checklist("g")
    assert checklist is not None
    assert [i.id for i in checklist.items] == ["cf-impl", "cf-tests"]
    assert any("firmed" in m for m in notifier.sent)


@pytest.mark.asyncio
async def test_can_run_gates_on_state(store_with_goal):
    """The handler short-circuits when the goal already has a firmed-draft on
    disk (firming round 1 only fires once), when the goal is blocked (waiting
    on owner answers — fired via handle_answer, not run), and when lifecycle
    is not firming at all."""
    store = store_with_goal
    handler = FirmingHandler(caller=FakeClaude(DRAFT_FIRMED))
    goal = store.load_goal("g")

    # baseline: lifecycle=firming, no draft → True
    assert await handler.can_run(goal, store.load_status("g"), store) is True

    # after a draft is written → False (round 1 has already run)
    store.write_firmed_draft("g", parse_firmed(DRAFT_FIRMED))
    assert await handler.can_run(goal, store.load_status("g"), store) is False

    # blocked goal → False even if draft is absent
    (next(iter([store.read_firmed_draft("g")])))  # touch
    store.save_status("g", GoalStatus(lifecycle="firming", phase="blocked"))
    assert await handler.can_run(goal, store.load_status("g"), store) is False


@pytest.mark.asyncio
async def test_handle_answer_round_2_firmed(store_with_goal):
    """handle_answer with answers that cover the prior round's unknowns → the
    model emits a firmed draft → service-level return shape carries status,
    round, and an empty unknowns list. Lifecycle flips to executing."""
    store = store_with_goal
    notifier = RecordingNotifier()
    # seed an existing round-1 draft with unknowns
    store.write_firmed_draft("g", parse_firmed(DRAFT_WITH_UNKNOWNS))
    store.save_status(
        "g", GoalStatus(lifecycle="firming", phase="blocked", blocked_on="1 question"),
    )
    firming = FakeClaude(response=DRAFT_FIRMED, role="goal_firming")
    decomposer = FakeClaude(response=DECOMPOSER_CHECKLIST_YAML, role="goal_decomposer")
    handler = FirmingHandler(caller=firming, decomposer_caller=decomposer)

    result = await handler.handle_answer(
        "g", {"cf-u1": "calendar_month"}, ctx=_ctx(store, notifier),
    )

    assert result["status"] == "firmed"
    assert result["round"] == 2
    assert result["unknowns"] == []
    # the prompt the model saw should include the owner answer + the prior draft
    assert "cf-u1: calendar_month" in firming.last_prompt
    assert "needs_owner_answers" in firming.last_prompt  # the prior draft body
    # lifecycle advanced AND the decomposer fired off the firmed goal
    after = store.load_status("g")
    assert after.lifecycle == "executing"
    assert after.phase == "idle"
    assert decomposer.calls == 1
    assert store.read_checklist("g") is not None


@pytest.mark.asyncio
async def test_handle_answer_surfaces_new_unknowns_keeps_goal_blocked(store_with_goal):
    """An owner answer can EXPOSE a new unknown (the round-2 prompt re-checks
    the merged state). When that happens, the goal stays parked at
    phase=blocked with the NEW unknowns; the return shape carries
    needs_more_answers + the new list."""
    store = store_with_goal
    notifier = RecordingNotifier()
    store.write_firmed_draft("g", parse_firmed(DRAFT_WITH_UNKNOWNS))
    store.save_status(
        "g", GoalStatus(lifecycle="firming", phase="blocked", blocked_on="1 question"),
    )
    second_round_with_new_unknown = """\
status: needs_owner_answers
round: 2
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns:
  - id: cf-u2
    question: Should we expose the report via REST or MCP only?
    why: Calendar-month choice unlocked the delivery question.
    options: [rest, mcp_only]
"""
    handler = FirmingHandler(caller=FakeClaude(second_round_with_new_unknown))

    result = await handler.handle_answer(
        "g", {"cf-u1": "calendar_month"}, ctx=_ctx(store, notifier),
    )

    assert result["status"] == "needs_more_answers"
    assert result["round"] == 2
    assert [u["id"] for u in result["unknowns"]] == ["cf-u2"]
    after = store.load_status("g")
    assert after.lifecycle == "firming"
    assert after.phase == "blocked"


# ---- firming → decomposer wiring (gap closure 2026-06-27) -----------------


@pytest.mark.asyncio
async def test_decomposer_failure_does_not_wedge_the_goal(store_with_goal):
    """If the decomposer cognition produces invalid output, firming logs the
    failure and STILL advances the goal to executing — same graceful-degrade
    pattern as the legacy `_resolve_discovery` path. The executor falls back
    to backlog mode (no checklist on disk). A wedge here would re-introduce
    the silent-stall failure mode firming was supposed to remove."""
    store = store_with_goal
    notifier = RecordingNotifier()
    firming = FakeClaude(response=DRAFT_FIRMED, role="goal_firming")
    # decomposer returns junk that fails the schema contract → GoalDecomposerError
    decomposer = FakeClaude(response="this is not yaml", role="goal_decomposer")
    handler = FirmingHandler(caller=firming, decomposer_caller=decomposer)
    goal = store.load_goal("g")
    status = store.load_status("g")

    result = await handler.run("g", goal, status, _ctx(store, notifier))

    # the goal still advanced — degrade, not wedge
    assert result.outcome == "advanced"
    after = store.load_status("g")
    assert after.lifecycle == "executing"
    assert after.phase == "idle"
    # no checklist on disk (backlog-mode fallback)
    assert store.read_checklist("g") is None


@pytest.mark.asyncio
async def test_firming_carries_stub_acceptable_into_decomposer(tmp_path):
    """When the firming draft populates `stub_acceptable` (owner authorized
    some stubs via Q&A), the synthesized goal passed to the decomposer must
    carry those authorizations — otherwise the decomposer would plan real
    work for capabilities the owner already agreed to stub."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="firming"))
    notifier = RecordingNotifier()
    firmed_with_stubs = """\
status: firmed
round: 2
intent: ship the mcp surface
success_criteria:
  - id: cf-1
    text: tools wired
    verifiable_by: ToolParityTests
unknowns: []
stub_acceptable:
  - get_cashflow_report
  - get_tax_lots
"""
    firming = FakeClaude(response=firmed_with_stubs, role="goal_firming")
    captured: dict = {}

    async def capture_decomposer(prompt: str) -> str:
        captured["prompt"] = prompt
        return DECOMPOSER_CHECKLIST_YAML

    handler = FirmingHandler(caller=firming, decomposer_caller=capture_decomposer)
    goal = store.load_goal("g")
    status = store.load_status("g")

    await handler.run("g", goal, status, _ctx(store, notifier))

    # the decomposer prompt's `## Goal` block carries the firmed
    # `stub_acceptable` — the decomposer's stub-policy rule reads this and
    # only authorizes stubs the owner listed.
    assert "get_cashflow_report" in captured["prompt"]
    assert "get_tax_lots" in captured["prompt"]


@pytest.mark.asyncio
async def test_firming_carries_conventions_blockers_descoped_into_decomposer(tmp_path):
    """When the firming draft populates conventions / blockers / descoped, the
    decomposer's prompt receives them as a postfix on the discovery brief so
    the planning respects existing patterns, scaffolds missing capabilities
    explicitly, and stays out of out-of-scope work."""
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(lifecycle="firming"))
    store.write_discovery("g", "## Current state\nthe baseline brief")
    notifier = RecordingNotifier()
    rich_firmed = """\
status: firmed
round: 2
intent: build the cashflow report
success_criteria:
  - id: cf-1
    text: report aggregates by calendar month
    verifiable_by: CashflowReportTests.GroupsByMonth
unknowns: []
conventions_to_follow:
  - CQRS via IQueryHandler<TQuery,TResult>
  - EF Core code-first migrations under Modules/*/Migrations/
blockers:
  - no shared aggregation utility — first user must build it
descoped:
  - per-day granularity in v1
"""
    firming = FakeClaude(response=rich_firmed, role="goal_firming")
    captured: dict = {}

    async def capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return DECOMPOSER_CHECKLIST_YAML

    handler = FirmingHandler(caller=firming, decomposer_caller=capture)
    goal = store.load_goal("g")
    status = store.load_status("g")
    await handler.run("g", goal, status, _ctx(store, notifier))

    prompt = captured["prompt"]
    # the baseline discovery brief still flows through
    assert "the baseline brief" in prompt
    # each firmed-extras section is rendered with its labeled header
    assert "Conventions to follow" in prompt
    assert "IQueryHandler" in prompt
    assert "Blockers" in prompt
    assert "shared aggregation utility" in prompt
    assert "Descoped" in prompt
    assert "per-day granularity" in prompt
