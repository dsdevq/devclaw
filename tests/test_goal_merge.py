"""Auto-merge is best-effort and guarded — an empty url is a no-op (never shells
out to gh), so a delivery with no PR can't trigger a merge attempt."""

from __future__ import annotations

import pytest

from devclaw.goal_merge import merge_pr


@pytest.mark.asyncio
async def test_empty_url_is_a_noop():
    assert await merge_pr("") is False
