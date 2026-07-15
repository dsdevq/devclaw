"""Goal-wrapper tests for the in-sandbox runner.

`implement_feature` used to pass the raw goal straight through, so the engineer
started blind on an existing repo — no read of the project's conventions, no
self-verification. The wrappers now brief it and tell it to verify. These pin
that behavior. The runner lives at openhands-runner/runner.py (not a package);
its openhands-sdk imports are inside main(), so a top-level import is SDK-free.
"""

import importlib.util
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_under_test", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # executes top-level only; main() is not __main__
    return mod


def test_implement_feature_is_no_longer_a_bare_passthrough(runner):
    wrapped = runner._wrap_goal("implement_feature", "add a /health endpoint")
    assert wrapped != "add a /health endpoint"  # the old behavior we fixed
    assert "add a /health endpoint" in wrapped  # the goal still rides along


def test_implement_feature_briefs_and_asks_to_verify(runner):
    wrapped = runner._wrap_goal("implement_feature", "GOAL-TOKEN")
    # briefed on the repo's own guide
    for cue in ("AGENTS.md", "CLAUDE.md", "README"):
        assert cue in wrapped
    # told to self-verify
    assert "VERIFY" in wrapped
    assert "test/build" in wrapped
    assert "GOAL-TOKEN" in wrapped  # the goal still rides along
    # the structured return contract is now the FINAL section (see below)
    assert wrapped.rstrip().endswith("with how far you got.")


def test_fix_bug_keeps_smallest_change_and_verifies(runner):
    wrapped = runner._wrap_goal("fix_bug", "BUG-TOKEN")
    assert "smallest change" in wrapped
    assert "VERIFY" in wrapped
    assert "AGENTS.md" in wrapped  # same project-guide briefing as implement_feature
    assert "BUG-TOKEN" in wrapped


def test_feature_and_fix_carry_a_code_quality_bar(runner):
    # the brief now demands production-quality code, not just a green suite — the
    # fix for "the agent only ever ships a working version"
    for kind in ("implement_feature", "fix_bug"):
        w = runner._wrap_goal(kind, "X").lower()
        assert "production code-quality" in w
        assert "no-op" in w                       # no dead/no-op code
        assert "necessary but not sufficient" in w  # green gate != good code
        assert "senior engineer" in w             # re-read your own diff critically


def test_quality_bar_is_only_for_code_changes(runner):
    # read-only review + onboarding don't write feature code, so they don't get it
    for kind in ("review_repository", "onboard"):
        assert "no-op" not in runner._wrap_goal(kind, "X").lower()


def test_review_repository_stays_read_only(runner):
    wrapped = runner._wrap_goal("review_repository", "look at auth")
    assert "READ ONLY" in wrapped
    assert "Do NOT modify" in wrapped
    assert "look at auth" in wrapped


# ---- structured return contract (task-brief structure work) -----------------


def test_code_tasks_end_with_a_structured_return_contract(runner):
    # The bare "say DONE" is replaced by a required, parseable hand-back so the
    # goal layer gets a legible account of what shipped. Present for both
    # code-writing kinds, and it is the LAST section (closing instruction).
    for kind in ("implement_feature", "fix_bug"):
        wrapped = runner._wrap_goal(kind, "GOAL-TOKEN")
        for field in ("STATUS:", "CHANGED:", "VERIFIED:", "ACCEPTANCE:", "FOLLOW-UPS:"):
            assert field in wrapped, f"{field} missing for {kind}"
        assert "GOAL-TOKEN" in wrapped  # the goal still rides along
        assert wrapped.index("GOAL-TOKEN") < wrapped.index("STATUS:")  # contract is last


def test_return_contract_not_added_to_read_only_kinds(runner):
    # review_repository + onboard have their OWN report contract (a written
    # report / doc set) — the code hand-back would fight it, so it's absent.
    # Proven absent from the raw wrapper first: the fields appear nowhere in the
    # review/onboard templates.
    for kind in ("review_repository", "onboard"):
        assert "ACCEPTANCE:" not in runner._KIND_WRAPPERS[kind]  # not in the raw template
        assert "FOLLOW-UPS:" not in runner._wrap_goal(kind, "X")  # nor in the rendered brief


def test_return_contract_reports_outcome_never_prescribes_how(runner):
    # The contract must not fight _QUALITY_BAR's "form your own opinion as a
    # senior engineer": it asks for a hand-back of OUTCOMES, not a recipe.
    contract = runner._RETURN_CONTRACT
    assert "acceptance criterion" in contract.lower()
    assert "only checks you truly ran" in contract  # factual, not aspirational


def test_unknown_kind_falls_back_to_implement_feature(runner):
    # unchanged contract: an unknown kind uses the implement_feature wrapper
    assert runner._wrap_goal("frobnicate", "X") == runner._wrap_goal(
        "implement_feature", "X"
    )


def test_implement_feature_asks_for_a_clean_self_authored_commit(runner):
    # The engineer writes its OWN conventional commit (so the delivered PR
    # describes the change, not the instruction), and does NOT push/PR itself.
    wrapped = runner._wrap_goal("implement_feature", "GOAL-TOKEN")
    assert "conventional-commit" in wrapped.lower()
    assert "COMMIT" in wrapped
    assert "do not push" in wrapped.lower() and "pull request" in wrapped.lower()


def test_wrapper_makes_agents_md_the_accumulated_harness(runner):
    # AGENTS.md is read FIRST and kept current as the reusable knowledge harness —
    # so future tasks don't re-derive the same context (token efficiency).
    wrapped = runner._wrap_goal("implement_feature", "x")
    assert "AGENTS.md" in wrapped
    assert "keep it current" in wrapped.lower()
    assert "re-derive" in wrapped.lower()
