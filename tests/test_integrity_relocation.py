"""Regression tests for the test-integrity RELOCATION CREDIT (2026-07-17).

A pure-deletion PR that removes a test file whose methods a PRIOR merged PR
already ported into another file shows N removals / 0 additions in its own diff
— which the count alone reads as a weakening. That mislabelling cost
closeloop-bench ~40h of thrash. These assert the gate now CREDITS a removed test
whose name is proven to still exist elsewhere in the post-change tree, while a
genuine deletion (no equivalent) stays flagged — grounded relaxation, fail
closed on no evidence.
"""

from __future__ import annotations

from devclaw.loom.test_integrity import present_test_names
from devclaw.task_queue import _integrity_failure

# A deletion diff removing two xUnit tests from the old duplicate file.
_DELETION_DIFF = (
    "diff --git a/backend/Tests/Domain/DealTests.cs b/backend/Tests/Domain/DealTests.cs\n"
    "deleted file mode 100644\n"
    "--- a/backend/Tests/Domain/DealTests.cs\n"
    "+++ /dev/null\n"
    "@@ -1,12 +0,0 @@\n"
    "-namespace Backend.Tests.Domain;\n"
    "-public class DealTests\n"
    "-{\n"
    "-    [Fact]\n"
    "-    public void Deal_Requires_Amount()\n"
    "-    {\n"
    "-        Assert.Throws<ArgumentException>(() => new Deal(0));\n"
    "-    }\n"
    "-    [Fact]\n"
    "-    public void Deal_Closes_When_Won()\n"
    "-    {\n"
    "-    }\n"
    "-}\n"
)


def _ported_equivalents(tmp_path) -> str:
    """A post-change tree where Domain.Tests/ already holds the ported methods
    (as a prior merged PR would have left them)."""
    d = tmp_path / "backend" / "Domain.Tests"
    d.mkdir(parents=True)
    (d / "DealTests.cs").write_text(
        "namespace Backend.Domain.Tests;\n"
        "public class DealTests\n"
        "{\n"
        "    // ported from backend/Tests/Domain/DealTests.cs (PR #3)\n"
        "    [Fact]\n"
        "    public void Deal_Requires_Amount() { }\n"
        "    [Fact]\n"
        "    public void Deal_Closes_When_Won() { }\n"
        "}\n"
    )
    return str(tmp_path)


def test_deletion_is_credited_when_equivalents_exist_elsewhere(tmp_path):
    ws = _ported_equivalents(tmp_path)
    # both removed methods live in Domain.Tests/ → move, not weakening → pass.
    assert _integrity_failure(_DELETION_DIFF, ws) is None


def test_present_test_names_finds_the_ported_methods(tmp_path):
    ws = _ported_equivalents(tmp_path)
    names = present_test_names(ws)
    assert {"Deal_Requires_Amount", "Deal_Closes_When_Won"} <= names


def test_genuine_deletion_with_no_equivalent_still_fails(tmp_path):
    # the tree has ONLY one of the two removed tests → the other is a real loss.
    d = tmp_path / "backend" / "Domain.Tests"
    d.mkdir(parents=True)
    (d / "DealTests.cs").write_text(
        "public class DealTests { [Fact] public void Deal_Requires_Amount() { } }\n"
    )
    reason = _integrity_failure(_DELETION_DIFF, str(tmp_path))
    assert reason is not None
    assert "1 test function(s) removed" in reason  # only the uncredited one remains


def test_no_workspace_cannot_credit_stays_flagged():
    # backward-compat: without a tree to check, a removal is flagged as before.
    reason = _integrity_failure(_DELETION_DIFF, None)
    assert reason is not None
    assert "2 test function(s) removed" in reason


def test_empty_tree_credits_nothing(tmp_path):
    # a walk that finds no equivalents must NOT relax the gate (fail closed).
    reason = _integrity_failure(_DELETION_DIFF, str(tmp_path))
    assert reason is not None and "2 test function(s) removed" in reason
