"""Direction evaluator — the JSON contract + verdict-mapping safety nets."""

from __future__ import annotations

import json

import pytest

from devclaw.goal.evaluator import GoalEvalError, build_prompt, evaluate, extract_json, validate
from devclaw.goal.models import Goal, GoalStatus


def test_extract_and_bad_verdict():
    assert '"verdict"' in extract_json('{"verdict":"achieved"}')
    with pytest.raises(GoalEvalError):
        validate({"verdict": "vibes"})
    with pytest.raises(GoalEvalError):
        validate("not a dict")


def test_each_valid_verdict():
    for v in ("on_track", "achieved", "stalled"):
        assert validate({"verdict": v, "rationale": "x"}).verdict == v


def test_off_track_requires_corrections_else_softened():
    # off_track WITH corrections stays off_track
    r = validate({"verdict": "off_track", "rationale": "drifting", "corrections": ["redo X"]})
    assert r.verdict == "off_track"
    assert r.corrections == ["redo X"]
    # off_track with NO corrections is softened to on_track (not actionable)
    r2 = validate({"verdict": "off_track", "rationale": "meh", "corrections": []})
    assert r2.verdict == "on_track"


def test_needs_human_backfills_question_from_rationale():
    r = validate({"verdict": "needs_human", "rationale": "which cloud provider?"})
    assert r.verdict == "needs_human"
    assert r.question == "which cloud provider?"


def _goal():
    return Goal(
        id="g", objective="ship a health endpoint", cadence="1d", engine="devclaw",
        workspace_dir="/ws", done_when="/health returns 200 and is tested",
        backlog=["add /health"],
    )


def test_done_gate_prompt_includes_review():
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report="the repo has /health and a passing test", at_done_gate=True,
    )
    assert "DONE-GATE" in prompt
    assert "read-only review" in prompt.lower()
    assert "the repo has /health" in prompt


@pytest.mark.asyncio
async def test_evaluate_roundtrip_with_injected_caller():
    calls = {"n": 0}

    async def caller(prompt: str) -> str:
        calls["n"] += 1
        return json.dumps({"verdict": "achieved", "rationale": "done_when met: /health tested"})

    ev = await evaluate(_goal(), GoalStatus(), "log", "deliveries", claude_caller=caller)
    assert ev.verdict == "achieved"
    assert calls["n"] == 1


def test_review_report_extraction_skips_prompt_template_and_uses_filled_section():
    """The worker's captured stdout starts with a panel echoing the agent's
    brief — which itself contains the literal template ``## Per-clause
    evidence`` followed by ``1. <clause 1 text>`` placeholders. The truncation
    must skip past that template and find the LAST (filled-in) per-clause
    section. Otherwise the evaluator reads the empty template + early
    `status=pending` tool-calls and reports 'review was cut off
    mid-exploration' (closeloop-ui-coverage 2026-06-28 incident)."""
    template = (
        "Message from User panel\n"
        "## Per-clause evidence\n"
        "1. <clause 1 text>\n"
        "   satisfied: yes | no | partial\n"
        "   evidence: <specific files/symbols/tests>\n"
    )
    decoration = "ACP Tool Call\nls -la /workspace\nstatus=pending\n" * 50
    actual_report = (
        "## Per-clause evidence\n"
        "1. /health endpoint exists\n"
        "   satisfied: yes\n"
        "   evidence: app/routes.py:42 health_handler covered by tests/test_health.py:8\n"
        "\n## Summary\nAll clauses satisfied.\n"
    )
    raw = template + decoration + actual_report
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report=raw, at_done_gate=True,
    )
    # the actual evidence — specific file/line — reaches the evaluator
    assert "app/routes.py:42 health_handler" in prompt
    assert "Summary" in prompt
    # the early `status=pending` decoration is excluded from the head-truncation
    assert "status=pending" not in prompt


def test_review_report_extraction_falls_back_to_tail_when_no_header():
    """Truly cut-off runs (no ``## Per-clause evidence`` ever emitted) must
    still surface SOME signal — the tail, where the most recent tool-call
    output and any partial work-in-progress lives. Head-truncation always
    showed only banner + prompt echo, never the agent's actual exploration."""
    early_banner = "Message from User\n" + ("x" * 5000)
    actual_work_at_end = (
        "I started exploring but ran into a permissions error reading "
        "/workspace/.env — DETAILS HERE for the evaluator to act on."
    )
    raw = early_banner + actual_work_at_end
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report=raw, at_done_gate=True,
    )
    assert "DETAILS HERE for the evaluator to act on" in prompt


def test_review_report_extraction_handles_empty_input():
    """Defensive: an empty / None review_report path must not crash and must
    not inject an empty section header into the prompt."""
    from devclaw.goal.evaluator import _extract_review_report
    assert _extract_review_report("") == ""


def test_done_gate_prompt_includes_spec_when_present():
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries",
        review_report="repo has /health", at_done_gate=True,
        spec="MUST expose /health AND /ready; auth required.",
    )
    assert "Agreed spec" in prompt and "/ready" in prompt


# ---- per-clause evidence contract (the 2026-06-25 trash-PR safety net) ----


def test_done_gate_prompt_carries_structural_health_axis():
    """The done-gate evaluator must consider TWO axes: functional clauses AND
    structural health. Without the second axis, a goal can verdict ``achieved``
    while leaving the codebase worse than before (closeloop App.tsx grew to
    1827 LOC through 4 such PRs in late June 2026). The prompt now tells the
    evaluator: both axes must pass before returning achieved."""
    prompt = build_prompt(
        _goal(), GoalStatus(), "log", "deliveries", at_done_gate=True,
        review_report=(
            "## Per-clause evidence\n1. health\n   satisfied: yes\n   evidence: app/main.py\n"
            "## Structural health\nverdict: clean\nNo concerns.\n"
        ),
    )
    text = prompt.lower()
    assert "structural" in text
    assert "## structural health" in prompt.lower() or "structural health" in text
    assert "both axes" in text  # the load-bearing rule
    # The exemplar that motivates the second axis must be named so the model
    # remembers WHY it's grading structure, not just THAT it should.
    assert "1827" in prompt or "monolith" in text or "closeloop" in text


def test_goal_evaluator_prompt_carries_clause_decomposition_directive():
    """The prompt MUST tell the model to decompose done_when into atomic
    clauses and demand specific repo evidence per clause — this is the
    behaviour that prevents the 'all stubs counted as done' failure mode."""
    prompt = build_prompt(_goal(), GoalStatus(), "log", "deliveries", at_done_gate=True)
    assert "DECOMPOSE" in prompt or "decompose" in prompt.lower()
    assert "atomic clauses" in prompt.lower() or "atomic clause" in prompt.lower()
    assert "evidence" in prompt.lower()
    # the strict rule that prevents the trash-PR class
    assert "achieved" in prompt.lower()


def test_done_gate_achieved_without_clauses_is_downgraded():
    # Belt-and-suspenders: even with a strict prompt the model can claim
    # 'achieved' without producing per-clause evidence. The validator
    # downgrades to off_track with a forcing correction.
    r = validate(
        {"verdict": "achieved", "rationale": "looks good"},
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert r.corrections, "expected a forcing correction asking for clauses"
    assert "clause" in r.corrections[0].lower()


def test_done_gate_achieved_with_unsatisfied_clause_is_downgraded():
    # 'achieved' with at least one unsatisfied clause must downgrade and
    # surface a per-clause correction.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "shipped",
            "clauses": [
                {
                    "clause": "/health returns 200",
                    "satisfied": True,
                    "evidence": "src/Health.cs:12 returns OK; HealthTests.cs:8 asserts 200",
                },
                {
                    "clause": "/health is tested",
                    "satisfied": False,
                    "evidence": "missing — should live in tests/HealthTests.cs",
                },
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    # the unsatisfied clause must surface as a correction
    assert any("/health is tested" in c for c in r.corrections)
    # clauses are preserved on the result for downstream visibility
    assert len(r.clauses) == 2
    assert r.clauses[1].satisfied is False


def test_done_gate_achieved_with_partial_evidence_is_downgraded():
    # 'partial' (string) coerces to satisfied=False — partial doesn't count.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "mostly",
            "clauses": [
                {"clause": "feature A", "satisfied": "yes", "evidence": "src/A.cs"},
                {"clause": "feature B", "satisfied": "partial", "evidence": "src/B.cs (incomplete)"},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert any("feature B" in c for c in r.corrections)


def test_done_gate_achieved_clause_with_no_evidence_is_downgraded():
    # satisfied=True but empty evidence → still downgraded (evidence contract).
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "shipped",
            "clauses": [
                {"clause": "feature A", "satisfied": True, "evidence": ""},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert any("feature A" in c for c in r.corrections)


def test_done_gate_achieved_with_all_clauses_satisfied_stays_achieved():
    # The HAPPY path: every clause satisfied with real evidence → achieved.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "all clauses met",
            "clauses": [
                {
                    "clause": "/health returns 200",
                    "satisfied": True,
                    "evidence": "src/Health.cs:12; HealthTests.cs:8",
                },
                {
                    "clause": "/health is tested",
                    "satisfied": True,
                    "evidence": "HealthTests.cs:8 Health_Returns200",
                },
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "achieved"
    assert len(r.clauses) == 2
    assert all(c.satisfied for c in r.clauses)


def test_pre_done_gate_achieved_is_not_strict():
    # Outside the done-gate, achieved doesn't require clauses (mid-goal
    # evaluator never returns achieved in practice, but the validator must
    # not reject it). Behaviour stays as the existing soft contract.
    r = validate({"verdict": "achieved", "rationale": "wip"})
    assert r.verdict == "achieved"


def test_off_track_at_done_gate_preserves_clauses():
    # When the model itself returns off_track (with corrections), the clauses
    # it produced are still preserved for visibility.
    r = validate(
        {
            "verdict": "off_track",
            "rationale": "one clause missing",
            "corrections": ["[clause 2] add the missing endpoint"],
            "clauses": [
                {"clause": "feature A", "satisfied": True, "evidence": "src/A.cs"},
                {"clause": "feature B", "satisfied": False, "evidence": "missing"},
            ],
        },
        at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert len(r.clauses) == 2
    assert r.corrections == ["[clause 2] add the missing endpoint"]


def test_eval_prompt_omits_spec_section_when_absent():
    prompt = build_prompt(_goal(), GoalStatus(), "log", "deliveries")
    assert "Agreed spec" not in prompt


# ---- stub-policy enforcement (the 2026-06-26 v5 safety net) ---------------
#
# Backstory: finance-sentry-mcp-v5 shipped 4 `not_yet_available` stubs for
# capabilities the repo didn't have (cashflow, crypto-pnl, tax-lots, net-worth-
# history). Every per-item gate passed (narrow verify_cmd) and the done-gate
# evaluator stamped them satisfied because the prompt previously endorsed
# "legitimate stubs". The fix: stubs are only acceptable when the goal's
# `stub_acceptable` list NAMES the tool. The validator enforces this
# mechanically as a belt-and-suspenders backup to the prompt rule.


def _goal_with_stub_acceptable(allowed: list[str]) -> Goal:
    return Goal(
        id="g", objective="ship a finance MCP", cadence="1d", engine="devclaw",
        workspace_dir="/ws",
        done_when=(
            "expose get_account_summary, get_cashflow_report, and get_tax_lots "
            "as MCP tools backed by authoritative reads"
        ),
        backlog=["scaffold mcp", "wire tools"],
        stub_acceptable=allowed,
    )


def test_eval_prompt_renders_stub_acceptable_block_when_populated():
    prompt = build_prompt(
        _goal_with_stub_acceptable(["get_cashflow_report", "get_tax_lots"]),
        GoalStatus(), "log", "deliveries", at_done_gate=True,
    )
    assert "stub_acceptable" in prompt
    assert "get_cashflow_report" in prompt
    assert "get_tax_lots" in prompt


def test_eval_prompt_warns_when_stub_acceptable_empty():
    # The empty case must be LOUD — the prompt actively warns the model so it
    # doesn't fall back to "stubs are basically fine" priors.
    prompt = build_prompt(_goal(), GoalStatus(), "log", "deliveries", at_done_gate=True)
    assert "stub_acceptable" in prompt
    assert "empty" in prompt.lower()
    assert "not authorized" in prompt.lower()


def test_unauthorized_stub_clause_is_downgraded_at_done_gate():
    # The exact v5 failure pattern: model returns satisfied=True for a clause
    # whose ONLY evidence is a not_yet_available stub. No stub_acceptable on
    # the goal → validator must flip it and surface the policy violation in
    # the correction.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "all 3 tools implemented",
            "clauses": [
                {
                    "clause": "get_account_summary returns authoritative data",
                    "satisfied": True,
                    "evidence": "Tools/GetAccountSummaryTool.cs:14 dispatches IBankingAccountsReader",
                },
                {
                    "clause": "get_cashflow_report returns a cashflow report",
                    "satisfied": True,
                    "evidence": "Tools/Stubs/CashflowReportStub.cs:14 returns NotYetAvailablePayload(\"not_yet_available\", \"...\")",
                },
            ],
        },
        at_done_gate=True,
        stub_acceptable=[],
    )
    assert r.verdict == "off_track"
    # the stub clause is flipped to unsatisfied with the policy reason
    cashflow = next(c for c in r.clauses if "cashflow" in c.clause)
    assert cashflow.satisfied is False
    assert "unauthorized stub" in cashflow.evidence.lower()
    # the real clause is preserved untouched
    summary = next(c for c in r.clauses if "summary" in c.clause)
    assert summary.satisfied is True
    # the correction names the unsatisfied clause so the planner can act
    assert any("cashflow" in c.lower() for c in r.corrections)


def test_authorized_stub_clause_stays_satisfied_at_done_gate():
    # Same shape as the previous test but stub_acceptable explicitly names
    # the cashflow tool → owner opted in → clause stays satisfied → verdict
    # remains achieved.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "1 real tool, 1 authorized stub",
            "clauses": [
                {
                    "clause": "get_account_summary returns authoritative data",
                    "satisfied": True,
                    "evidence": "Tools/GetAccountSummaryTool.cs:14 dispatches IBankingAccountsReader",
                },
                {
                    "clause": "get_cashflow_report returns a cashflow report",
                    "satisfied": True,
                    "evidence": "Tools/Stubs/CashflowReportStub.cs:14 returns NotYetAvailablePayload(\"not_yet_available\", \"...\")",
                },
            ],
        },
        at_done_gate=True,
        stub_acceptable=["get_cashflow_report"],
    )
    assert r.verdict == "achieved"
    assert all(c.satisfied for c in r.clauses)


def test_authorized_stub_matched_by_substring_not_just_exact():
    # Tool-slug authorization is substring (case-insensitive) — the clause
    # text says "get_tax_lots tool" not "get_tax_lots" verbatim; the
    # evidence is a *Stub class name. Both forms should be enough to match
    # the stub_acceptable entry.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "authorized stub",
            "clauses": [
                {
                    "clause": "the get_tax_lots tool is exposed",
                    "satisfied": True,
                    "evidence": "Stubs/TaxLotsStub.cs:9 returns not_yet_available",
                },
            ],
        },
        at_done_gate=True,
        stub_acceptable=["GET_TAX_LOTS"],  # case-insensitive
    )
    assert r.verdict == "achieved"


def test_stub_policy_no_op_when_no_stub_markers_in_evidence():
    # A clause whose evidence is real symbols (no stub markers) is
    # unaffected by the stub policy even if stub_acceptable is empty.
    r = validate(
        {
            "verdict": "achieved",
            "rationale": "real wiring",
            "clauses": [
                {
                    "clause": "get_account_summary returns data",
                    "satisfied": True,
                    "evidence": "Tools/GetAccountSummaryTool.cs:14 dispatches IBankingAccountsReader",
                },
            ],
        },
        at_done_gate=True,
        stub_acceptable=[],
    )
    assert r.verdict == "achieved"


def test_stub_policy_only_applies_at_done_gate():
    # Outside the done-gate the policy is dormant — pre-done-gate ticks
    # shouldn't downgrade evidence the planner is mid-shipping.
    r = validate(
        {
            "verdict": "achieved",  # nonsense pre-done-gate but accepted as-is
            "rationale": "wip",
            "clauses": [
                {
                    "clause": "get_cashflow_report",
                    "satisfied": True,
                    "evidence": "CashflowReportStub.cs returns not_yet_available",
                },
            ],
        },
        at_done_gate=False,
        stub_acceptable=[],
    )
    assert r.verdict == "achieved"


@pytest.mark.asyncio
async def test_evaluate_threads_stub_acceptable_through_to_validate():
    # End-to-end at the function level: evaluate() must pull stub_acceptable
    # off the goal and pass it to validate() — otherwise the policy is
    # unenforced in production despite the unit tests passing.
    goal = _goal_with_stub_acceptable([])  # no stubs allowed

    async def caller(_prompt: str) -> str:
        return json.dumps({
            "verdict": "achieved",
            "rationale": "shipped",
            "clauses": [
                {
                    "clause": "get_cashflow_report",
                    "satisfied": True,
                    "evidence": "CashflowReportStub.cs returns not_yet_available",
                },
            ],
        })

    r = await evaluate(
        goal, GoalStatus(), "log", "deliveries",
        claude_caller=caller, at_done_gate=True,
    )
    assert r.verdict == "off_track"
    assert "cashflow" in r.corrections[0].lower()


# ---- standing-goal contract (the 2026-07-06 benchmark safety net) ----------
#
# Backstory: closeloop-bench-2026-07-05's done_when read "Not applicable as a
# bounded criterion — this is a standing goal ... Fail any → off_track" and the
# done-gate still terminally closed it `achieved`. A standing goal is closed by
# the OWNER (cancel_goal / re-aim), never by the gate: an all-axes-pass verdict
# must become needs_human, which blocks + notifies instead of closing.


def _standing_goal() -> Goal:
    return Goal(
        id="g", objective="closeloop mirrors best-in-class CRMs", cadence="6h",
        engine="devclaw", workspace_dir="/ws",
        done_when=(
            "Not applicable as a bounded criterion — this is a standing goal. "
            "Judge each delivery against the four axes; fail any → off_track."
        ),
        backlog=["notifications engine"],
    )


_ALL_PASS_ACHIEVED = {
    "verdict": "achieved",
    "rationale": "all axes pass",
    "clauses": [
        {"clause": "research is real", "satisfied": True, "evidence": "docs/research/crm.md"},
        {"clause": "synthesis argued", "satisfied": True, "evidence": "docs/features/x.md Borrowed/Rejected"},
    ],
    "structural_health": "clean",
}


def test_is_standing_matches_contract_phrasings():
    from devclaw.goal.models import is_standing

    assert is_standing("this is a standing goal")
    assert is_standing("Not applicable as a bounded criterion — judge deliveries")
    assert is_standing("NOT A BOUNDED CRITERION")
    assert is_standing("there is no terminal state for this goal")
    # bounded contracts stay bounded
    assert not is_standing("/health returns 200 and is tested")
    assert not is_standing("all backlog items merged")
    assert not is_standing("")


def test_standing_done_gate_achieved_becomes_needs_human():
    r = validate(_ALL_PASS_ACHIEVED, at_done_gate=True, standing=True)
    assert r.verdict == "needs_human"
    assert "standing" in r.question.lower()
    # the grading survives the conversion — the owner sees WHAT passed
    assert len(r.clauses) == 2 and all(c.satisfied for c in r.clauses)
    assert r.structural_health == "clean"


def test_standing_does_not_soften_off_track():
    # standing only intercepts the CLOSE; a failing axis still steers as usual.
    r = validate(
        {
            "verdict": "off_track", "rationale": "axis 3 failed",
            "corrections": ["[clause 1] fix the JWT fallback"],
        },
        at_done_gate=True, standing=True,
    )
    assert r.verdict == "off_track"
    assert r.corrections == ["[clause 1] fix the JWT fallback"]


def test_non_standing_achieved_is_unaffected():
    r = validate(_ALL_PASS_ACHIEVED, at_done_gate=True, standing=False)
    assert r.verdict == "achieved"


def test_standing_prompt_carries_the_contract_note():
    prompt = build_prompt(_standing_goal(), GoalStatus(), "log", "deliveries", at_done_gate=True)
    assert "STANDING-GOAL CONTRACT" in prompt
    # bounded goals don't get the note
    bounded = build_prompt(_goal(), GoalStatus(), "log", "deliveries", at_done_gate=True)
    assert "STANDING-GOAL CONTRACT" not in bounded


@pytest.mark.asyncio
async def test_evaluate_threads_standing_through_to_validate():
    async def caller(prompt: str) -> str:
        return json.dumps(_ALL_PASS_ACHIEVED)

    r = await evaluate(
        _standing_goal(), GoalStatus(), "log", "deliveries",
        claude_caller=caller, at_done_gate=True,
    )
    assert r.verdict == "needs_human"
    assert "standing" in r.question.lower()
