"""Evaluator cognition evals.

Two layers:
- **Mechanism guards** (always on, ~ms, zero quota) — round-trip every fixture
  through the loader, ``build_prompt``, and ``validate`` on the canned model
  output.  Proves the fixture is coherent and the parsing layer keeps its
  expected verdict.
- **Live cognition run** (opt-in via ``DEVCLAW_RUN_COGNITION_EVALS=1``) — calls
  the REAL evaluator against Anthropic for each fixture and prints the verdict
  next to the fixture's expected verdict.  A human reads the output and judges;
  there is intentionally NO automated pass/fail here — premature automation is
  worse than none, per plan.md §Per-module evals.
"""
from __future__ import annotations

import pytest

from tests.cognition import harness


FIXTURES = harness.load_evaluator_fixtures()
FIXTURE_IDS = [f.name for f in FIXTURES]


@pytest.fixture(autouse=True)
def _reset_review_gate_flag(monkeypatch):
    """The suite-wide autouse conftest disables the review gate; these tests
    don't touch it, but reasserting keeps the intent visible if this file is
    ever run in isolation."""
    from devclaw import task_queue
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", False)


def test_at_least_three_fixtures_exist():
    """The scaffold ships with the three-fixture starter set from plan.md
    §Per-module evals; regressing below that is a signal the discipline is
    silently eroding."""
    assert len(FIXTURES) >= 3, (
        f"expected ≥3 evaluator fixtures, found {len(FIXTURES)}: {FIXTURE_IDS}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_loads_into_full_objects(fixture: harness.EvaluatorFixture):
    """Every fixture must materialize into real ``Goal`` + ``GoalStatus``
    objects — catches a broken schema before a live call spends quota on it."""
    assert fixture.goal.objective, "goal.objective must be non-empty"
    assert fixture.goal.done_when, "fixtures without done_when can't exercise the done-gate"
    assert fixture.status.phase in {"idle", "in_flight", "verifying", "blocked", "done", "cancelled"}
    assert fixture.expected_verdict in {
        "on_track", "off_track", "achieved", "stalled", "needs_human",
    }


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_prompt_is_well_formed(fixture: harness.EvaluatorFixture):
    """``build_prompt`` should produce a non-empty prompt that carries the goal
    context an evaluator needs.  Cheap sanity that the prompt template hasn't
    silently dropped a critical section."""
    prompt = harness.build_prompt_for(fixture)
    assert fixture.goal.objective in prompt
    assert fixture.goal.done_when in prompt
    if fixture.at_done_gate:
        assert "DONE-GATE" in prompt or "done-gate" in prompt.lower(), (
            "at_done_gate fixtures should surface the done-gate framing in the prompt"
        )
    if fixture.review_report:
        # The review-report tail is truncated to _REVIEW_REPORT_KEEP; enough of
        # its content should still be present that a substring check passes.
        assert "Per-clause evidence" in prompt, (
            "review_report fixture didn't propagate the per-clause header into the prompt"
        )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_canned_model_output_validates_to_expected_verdict(
    fixture: harness.EvaluatorFixture,
):
    """The fixture's ``canned_model_output`` is what a well-behaved model
    would return.  When fed through the REAL ``validate()`` layer it MUST
    reproduce ``expected.verdict`` — including the mechanical safety nets
    (unauthorized-stub flip, done-gate strictness, missing-evidence downgrade).
    If this fails the fixture is incoherent: the canned output the human wrote
    doesn't survive the parser."""
    result = harness.validate_canned(fixture)
    assert result.verdict == fixture.expected_verdict, (
        f"fixture {fixture.name!r}: canned model output validated to "
        f"{result.verdict!r} but fixture expects {fixture.expected_verdict!r}. "
        f"Notes: {fixture.expected_notes!r}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_expected_hint_substrings_show_up_on_downgrade(
    fixture: harness.EvaluatorFixture,
):
    """For fixtures whose canned output goes through a validate() downgrade
    (e.g. off_track_stub_disguise, off_track_missing_clause), the corrections
    should mention the substrings we said we expect — otherwise the fixture's
    hint list is drifting from what the parser actually surfaces."""
    if not fixture.expected_correction_hints:
        return  # no hints to check
    result = harness.validate_canned(fixture)
    if not result.corrections:
        return  # nothing to grep against — live run does the real judging
    blob = "\n".join(result.corrections).lower()
    for hint in fixture.expected_correction_hints:
        assert hint.lower() in blob, (
            f"fixture {fixture.name!r}: expected correction hint {hint!r} not "
            f"found in derived corrections: {result.corrections!r}"
        )


# ---------------------------------------------------------------------------
# Live cognition run — opt-in.  Skipped unless DEVCLAW_RUN_COGNITION_EVALS=1.
# ---------------------------------------------------------------------------

_LIVE_MARK = pytest.mark.skipif(
    not harness.cognition_evals_enabled(),
    reason="opt-in: set DEVCLAW_RUN_COGNITION_EVALS=1 to run live cognition evals",
)


@_LIVE_MARK
@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
async def test_live_cognition_run(fixture: harness.EvaluatorFixture, capsys):
    """Call the REAL evaluator against Anthropic and print the verdict next
    to the fixture's expected verdict.  There is no assert on quality — a
    human reads the output and decides.  Only fails if the call errors or
    returns something structurally invalid (which the evaluator's own
    validate() would have raised already)."""
    result = await harness.run_evaluator_live(fixture)
    with capsys.disabled():
        print()
        print("=" * 72)
        print(f"FIXTURE: {fixture.name}  ({fixture.source})")
        print(f"NOTES:   {fixture.notes}")
        print(f"EXPECTED verdict: {fixture.expected_verdict}")
        if fixture.expected_notes:
            print(f"EXPECTED reason:  {fixture.expected_notes}")
        print("-" * 72)
        print("LIVE MODEL OUTPUT:")
        print(harness.format_verdict(result))
        print("=" * 72)
    # Structural sanity only — no quality gate.
    assert result.verdict in {
        "on_track", "off_track", "achieved", "stalled", "needs_human",
    }
