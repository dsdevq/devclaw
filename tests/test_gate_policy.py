"""ADR 0007 — the pure gate-strictness policy function.

Exhaustive table: every (gate, strictness) failure maps to the right
consequence. The whole strict/trust policy lives in one function, so the whole
policy is pinned here.
"""

from __future__ import annotations

import pytest

from devclaw.quality.gate_policy import (
    ALWAYS_HARD,
    DIAL_ABLE,
    Consequence,
    gate_consequence,
)


@pytest.mark.parametrize("gate_id", sorted(ALWAYS_HARD))
@pytest.mark.parametrize("strictness", ["trust", "strict"])
def test_always_hard_gates_block_in_both_modes(gate_id, strictness):
    # test_integrity / delivery_trust / done_gate / verify guard against the
    # model gaming its own evidence — the dial must NEVER loosen them.
    assert gate_consequence(gate_id, strictness) is Consequence.BLOCK


@pytest.mark.parametrize("gate_id", sorted(DIAL_ABLE))
def test_dial_able_gate_advises_under_trust(gate_id):
    assert gate_consequence(gate_id, "trust") is Consequence.ADVISE


@pytest.mark.parametrize("gate_id", sorted(DIAL_ABLE))
def test_dial_able_gate_blocks_under_strict(gate_id):
    assert gate_consequence(gate_id, "strict") is Consequence.BLOCK


def test_unknown_strictness_fails_closed():
    # An unrecognized dial value must never silently loosen a gate — it blocks.
    assert gate_consequence("browser", "garbage") is Consequence.BLOCK
    assert gate_consequence("browser", "") is Consequence.BLOCK


def test_dial_able_and_always_hard_are_disjoint():
    # A gate is never both — the two review-shaped gates are exactly the ones
    # NOT in ALWAYS_HARD.
    assert ALWAYS_HARD.isdisjoint(DIAL_ABLE)
