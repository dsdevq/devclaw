"""reconcile_stack — the mechanism that settles a program's PR stack.

The decision table is the contract: superseded → close, green → merge (in
order), red/conflicting/unknown → left open with a reason the planner can act
on. Everything is best-effort — no branch may raise into the tick.
"""

from __future__ import annotations

import pytest

from devclaw.goal.reconcile import _checks_green, reconcile_stack


class FakeMerger:
    def __init__(self, ok=True):
        self.merged: list[str] = []
        self._ok = ok

    async def __call__(self, url: str) -> bool:
        self.merged.append(url)
        return self._ok


def _state(state="OPEN", mergeable="MERGEABLE", rollup=None):
    async def probe(url):
        return {"state": state, "mergeable": mergeable, "statusCheckRollup": rollup or []}
    return probe


def _superseded(value: bool):
    async def check(url, ws):
        return value
    return check


def _closer(ok=True, record=None):
    async def close(url):
        if record is not None:
            record.append(url)
        return ok
    return close


@pytest.mark.asyncio
async def test_green_stack_merges_in_order():
    merger = FakeMerger()
    summary = await reconcile_stack(
        ["u/66", "u/67", "u/68"], workspace_dir="/ws", merger=merger,
        pr_state=_state(), superseded=_superseded(False), closer=_closer(),
    )
    assert merger.merged == ["u/66", "u/67", "u/68"]
    assert all(s.endswith("merged") for s in summary)


@pytest.mark.asyncio
async def test_superseded_pr_is_closed_not_merged():
    merger, closed = FakeMerger(), []
    summary = await reconcile_stack(
        ["u/66"], workspace_dir="/ws", merger=merger,
        pr_state=_state(), superseded=_superseded(True), closer=_closer(record=closed),
    )
    assert closed == ["u/66"] and merger.merged == []
    assert "closed (superseded by main)" in summary[0]


@pytest.mark.asyncio
async def test_conflicting_and_red_checks_left_open():
    merger = FakeMerger()
    summary = await reconcile_stack(
        ["u/1", "u/2"], workspace_dir="/ws", merger=merger,
        pr_state=_state(mergeable="CONFLICTING"), superseded=_superseded(False), closer=_closer(),
    )
    assert merger.merged == []
    assert all("left open" in s for s in summary)

    red = _state(rollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    summary = await reconcile_stack(
        ["u/3"], workspace_dir="/ws", merger=merger,
        pr_state=red, superseded=_superseded(False), closer=_closer(),
    )
    assert merger.merged == [] and "checks red or pending" in summary[0]


@pytest.mark.asyncio
async def test_already_settled_and_probe_failure_are_skips():
    merger = FakeMerger()

    async def flaky(url):
        return {} if url == "u/dead" else {"state": "MERGED", "mergeable": "UNKNOWN"}

    summary = await reconcile_stack(
        ["u/dead", "u/done"], workspace_dir="/ws", merger=merger,
        pr_state=flaky, superseded=_superseded(False), closer=_closer(),
    )
    assert merger.merged == []
    assert "probe failed" in summary[0] and "already merged" in summary[1]


@pytest.mark.asyncio
async def test_pending_checks_block_merge():
    merger = FakeMerger()
    pending = _state(rollup=[{"status": "IN_PROGRESS", "conclusion": ""}])
    summary = await reconcile_stack(
        ["u/9"], workspace_dir="/ws", merger=merger,
        pr_state=pending, superseded=_superseded(False), closer=_closer(),
    )
    assert merger.merged == [] and "left open" in summary[0]


def test_checks_green_table():
    assert _checks_green([]) and _checks_green(None)
    assert _checks_green([{"status": "COMPLETED", "conclusion": "SUCCESS"},
                          {"status": "COMPLETED", "conclusion": "SKIPPED"}])
    assert not _checks_green([{"status": "COMPLETED", "conclusion": "FAILURE"}])
    assert not _checks_green([{"status": "QUEUED", "conclusion": ""}])
    assert not _checks_green("garbage")
