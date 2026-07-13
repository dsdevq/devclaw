"""Goal next-action planner — the JSON contract (folded from goalclaw)."""

from __future__ import annotations

import pytest

from devclaw.goal.models import Goal, GoalStatus
from devclaw.goal.planner import GoalPlannerError, build_prompt, extract_json, validate


def _goal():
    return Goal(id="g", objective="make it good", cadence="1d", engine="devclaw", workspace_dir="/repo")


def test_build_prompt_includes_discovery_when_present():
    p = build_prompt(_goal(), GoalStatus(), "", "", "", discovery="## Current state\nbare API")
    assert "Discovery brief" in p and "bare API" in p


def test_build_prompt_omits_discovery_section_when_absent():
    p = build_prompt(_goal(), GoalStatus(), "", "", "")
    assert "Discovery brief" not in p


# ---- live workspace grounding (triage F5) ----------------------------------


def test_goal_planner_prompt_carries_anti_inference_guard():
    """F5: the system prompt must forbid inferring repo facts (stack, layout,
    test runner, file paths) from anywhere but the grounded input sections —
    the host environment, the cwd host-side claude inherits from devclaw, and
    remembered repos are all off limits. Same shape as review-gate.md's guard
    (#227)."""
    p = build_prompt(_goal(), GoalStatus(), "", "", "")
    assert "Ground every repository fact in what you are given" in p
    assert "treat it as unknown" in p
    assert "your own working directory" in p
    assert "must never name a language" in p


def test_build_prompt_renders_repo_context_section_when_present():
    p = build_prompt(
        _goal(), GoalStatus(), "", "", "",
        repo_context="git_remote_origin: https://x/y.git\nglobal.json: file",
    )
    assert "Repository context (facts from the actual workspace" in p
    assert "git_remote_origin: https://x/y.git" in p


def test_build_prompt_omits_repo_context_section_when_empty():
    # "" (collector hiccup / best-effort degrade) skips the section — the
    # anti-inference guard in the system prompt still applies.
    p = build_prompt(_goal(), GoalStatus(), "", "", "", repo_context="")
    assert "facts from the actual workspace" not in p


# ---- trend signals (trend-PR3) --------------------------------------------


def test_build_prompt_includes_trends_when_present():
    trend = (
        "## [2026-06-29T15:00:00+00:00] R2 — reactive_agents_md_change\n\n"
        "AGENTS.md was patched only after the failure surfaced — reactive pattern."
    )
    p = build_prompt(
        _goal(), GoalStatus(), "", "", "",
        discovery="## Current state\nbare API",
        trends=trend,
    )
    assert "Trend signals (recent retrospective findings for this project)" in p
    assert "reactive_agents_md_change" in p
    # Ordering: after Discovery, before Checklist (rendered or not — we still
    # check trends comes after the discovery section header).
    assert p.index("Discovery brief") < p.index("Trend signals")


def test_build_prompt_omits_trends_section_when_empty():
    p = build_prompt(_goal(), GoalStatus(), "", "", "", trends="")
    assert "Trend signals" not in p


def test_build_prompt_trends_renders_between_discovery_and_checklist():
    cl = _cl(ChecklistItem(id="a", requirement="r", evidence_target="t"))
    p = build_prompt(
        _goal(), GoalStatus(), "", "", "",
        discovery="state",
        trends="## [t] R2 — recurrence\n\nbody",
        checklist=cl,
    )
    # All three sections rendered in the expected order. ``Checklist (ready
    # items)`` also appears in the loaded system prompt header — use rindex
    # to pin the rendered SECTION header, not the system-prompt mention.
    i_disc = p.index("Discovery brief")
    i_trend = p.index("Trend signals")
    i_check = p.rindex("Checklist (ready items)")
    assert i_disc < i_trend < i_check


# ---- checklist mode (Pillar 1) --------------------------------------------


from devclaw.goal.models import Checklist, ChecklistItem  # noqa: E402


def _cl(*items: ChecklistItem, open_questions=None, notes=None) -> Checklist:
    return Checklist(
        items=list(items),
        open_questions=list(open_questions or []),
        notes=list(notes or []),
    )


def test_build_prompt_omits_checklist_section_when_absent():
    p = build_prompt(_goal(), GoalStatus(), "", "", "")
    # the goal-planner.md system prompt mentions "Checklist" in its mode
    # description — substring matches that copy. Check for the rendered
    # section's load-bearing string instead (the tally line).
    assert "items total:" not in p


def test_build_prompt_omits_checklist_section_when_empty():
    p = build_prompt(_goal(), GoalStatus(), "", "", "", checklist=_cl())
    assert "items total:" not in p


def test_build_prompt_renders_ready_items_in_checklist_mode():
    cl = _cl(
        ChecklistItem(
            id="scaffold", requirement="Create the csproj.",
            evidence_target="backend/src/Foo.csproj",
        ),
        ChecklistItem(
            id="wire-x", requirement="Wire the X tool.",
            evidence_target="backend/src/Tools/X.cs",
            depends_on=["scaffold"],  # NOT ready yet — scaffold isn't done
        ),
    )
    p = build_prompt(_goal(), GoalStatus(), "", "", "", checklist=cl)
    assert "Checklist (ready items)" in p
    # scaffold is ready (no deps), wire-x is NOT ready (dep on scaffold)
    assert "scaffold: Create the csproj." in p
    assert "wire-x" not in p
    # the tally is rendered
    assert "items total: 2" in p


def test_build_prompt_includes_open_questions_and_notes_when_present():
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t"),
        open_questions=["Is X one tool or two?"],
        notes=["Contract test files overlap — serialize."],
    )
    p = build_prompt(_goal(), GoalStatus(), "", "", "", checklist=cl)
    assert "open questions" in p
    assert "Is X one tool or two?" in p
    assert "notes from the decomposer" in p
    assert "Contract test files overlap" in p


def test_build_prompt_warns_when_no_ready_items_but_not_all_done():
    # Every not_started item is blocked on something — planner should see
    # the explicit hint to propose done if everything else is shipped.
    cl = _cl(
        ChecklistItem(id="a", requirement="r", evidence_target="t", status="in_flight"),
        ChecklistItem(id="b", requirement="r", evidence_target="t", depends_on=["a"]),
    )
    p = build_prompt(_goal(), GoalStatus(), "", "", "", checklist=cl)
    assert "(none — every not_started item has unmet dependencies" in p


def test_validate_act_with_addresses_field():
    res = validate({
        "decision": "act",
        "note": "wire accounts",
        "actions": [{
            "tool": "implement_feature",
            "goal": "wire accounts tool to GetAccountsQuery",
            "open_pr": True,
            "addresses": ["wire-accounts"],
        }],
    })
    assert res.decision == "act"
    assert res.actions[0].addresses == ["wire-accounts"]


def test_validate_act_addresses_dedup_and_strip():
    res = validate({
        "decision": "act",
        "note": "n",
        "actions": [{
            "tool": "implement_feature",
            "goal": "do it",
            "open_pr": True,
            "addresses": ["wire-a", " wire-a ", "wire-b", "", "wire-a"],
        }],
    })
    assert res.actions[0].addresses == ["wire-a", "wire-b"]


def test_validate_act_addresses_absent_defaults_to_empty():
    # Legacy backlog-mode actions don't emit addresses — must still parse.
    res = validate({
        "decision": "act",
        "note": "n",
        "actions": [{"tool": "implement_feature", "goal": "do it", "open_pr": True}],
    })
    assert res.actions[0].addresses == []


def test_validate_act_addresses_garbage_ignored():
    # A model that returns a non-list addresses field is treated as empty,
    # not as a parse error — falls back to legacy behaviour.
    res = validate({
        "decision": "act",
        "note": "n",
        "actions": [{
            "tool": "implement_feature", "goal": "do it",
            "open_pr": True, "addresses": "not-a-list",
        }],
    })
    assert res.actions[0].addresses == []


def test_extract_json_plain():
    assert extract_json('{"decision":"sleep"}') == '{"decision":"sleep"}'


def test_extract_json_fenced():
    raw = "here you go:\n```json\n{\"decision\": \"sleep\"}\n```\n"
    assert '"decision"' in extract_json(raw)


def test_extract_json_none_raises():
    with pytest.raises(GoalPlannerError):
        extract_json("no json here")


def test_validate_act_one_action():
    res = validate(
        {
            "decision": "act",
            "note": "ship health endpoint",
            "actions": [{"tool": "implement_feature", "goal": "add /health", "open_pr": True}],
        }
    )
    assert res.decision == "act"
    assert len(res.actions) == 1
    assert res.actions[0].tool == "implement_feature"
    assert res.actions[0].goal == "add /health"
    assert res.actions[0].open_pr is True


def test_validate_act_rejects_multiple_actions():
    with pytest.raises(GoalPlannerError):
        validate(
            {
                "decision": "act",
                "actions": [
                    {"tool": "implement_feature", "goal": "a"},
                    {"tool": "fix_bug", "goal": "b"},
                ],
            }
        )


def test_validate_act_rejects_bad_tool():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "act", "actions": [{"tool": "rm_rf", "goal": "x"}]})


def test_validate_act_rejects_empty_goal():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "act", "actions": [{"tool": "fix_bug", "goal": "  "}]})


def test_validate_blocked_requires_question():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "blocked"})
    res = validate({"decision": "blocked", "question": "which auth provider?"})
    assert res.decision == "blocked"
    assert res.question == "which auth provider?"


def test_validate_sleep_and_done():
    assert validate({"decision": "sleep", "note": "nothing to do"}).decision == "sleep"
    assert validate({"decision": "done", "note": "all merged"}).decision == "done"


def test_validate_bad_decision():
    with pytest.raises(GoalPlannerError):
        validate({"decision": "explode"})
