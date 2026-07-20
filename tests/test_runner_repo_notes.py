"""Runner-side REPO NOTES parsing (mission-control borrow item 3).

The return contract asks code-writing workers for one line of durable
repo-level facts; the runner parses it off the agent's OWN final message
(same philosophy as the BLOCKED line: plain text, model-agnostic, no vendor
wiring) and rides it to the host as ``repo_notes`` on the result payload.
'none'/empty degrade to None — an unfilled field is the normal case.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_repo_notes", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; openhands imports live in main()
    return mod


def test_parse_repo_notes_extracts_the_field(runner):
    msg = (
        "STATUS: DONE\n"
        "CHANGED: added /health endpoint.\n"
        "VERIFIED: pytest -q green.\n"
        "ACCEPTANCE: none stated\n"
        "FOLLOW-UPS: none\n"
        "REPO NOTES: tests need `npm run test:ci`; build is pnpm-only\n"
    )
    assert runner._parse_repo_notes(msg) == "tests need `npm run test:ci`; build is pnpm-only"


def test_parse_repo_notes_none_and_absent_degrade_to_none(runner):
    assert runner._parse_repo_notes("STATUS: DONE\nREPO NOTES: none\n") is None
    assert runner._parse_repo_notes("STATUS: DONE\nREPO NOTES: None.\n") is None
    assert runner._parse_repo_notes("STATUS: DONE\nREPO NOTES:\n") is None
    assert runner._parse_repo_notes("STATUS: DONE\n") is None
    assert runner._parse_repo_notes(None) is None
    assert runner._parse_repo_notes("") is None


def test_parse_repo_notes_last_line_wins_and_markdown_is_tolerated(runner):
    msg = (
        "> REPO NOTES: an early draft\n"
        "some prose in between\n"
        "**REPO NOTES:** the final, corrected fact\n"
    )
    assert runner._parse_repo_notes(msg) == "the final, corrected fact"


def test_parse_repo_notes_mid_sentence_prose_cannot_false_positive(runner):
    msg = "I appended the repo notes: nothing else changed.\n"
    assert runner._parse_repo_notes(msg) is None


def test_return_contract_asks_for_repo_notes(runner):
    assert "REPO NOTES:" in runner._RETURN_CONTRACT
    # And the wrapped brief for a code-writing kind carries it…
    assert "REPO NOTES:" in runner._wrap_goal("implement_feature", "x")
    # …while read-only kinds keep their own report contract.
    assert "REPO NOTES:" not in runner._wrap_goal("review_repository", "x")


def test_blocked_handback_can_still_carry_repo_notes(runner):
    msg = (
        "STATUS: BLOCKED: the API contract is contradictory\n"
        "REPO NOTES: e2e suite requires the dev server on :4200\n"
    )
    assert runner._parse_blocked_reason(msg) == "the API contract is contradictory"
    assert runner._parse_repo_notes(msg) == "e2e suite requires the dev server on :4200"
