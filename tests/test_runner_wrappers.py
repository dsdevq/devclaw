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
    assert wrapped.rstrip().endswith("GOAL-TOKEN")  # goal lands last


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


def test_unknown_kind_falls_back_to_implement_feature(runner):
    # unchanged contract: an unknown kind uses the implement_feature wrapper
    assert runner._wrap_goal("frobnicate", "X") == runner._wrap_goal(
        "implement_feature", "X"
    )
