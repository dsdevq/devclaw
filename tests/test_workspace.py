"""Workspace prep — the clone/fetch/reset lifecycle that hands the engine a
pristine checkout. These cover the *failure* messages specifically: they become
a goal's ``blocked_on`` and are read by the (non-technical) owner, so they must
say what's wrong and what to do — not leak a bare assertion."""

from __future__ import annotations

import pytest

from devclaw.engine.workspace import WorkspaceError, prepare_workspace


@pytest.mark.asyncio
async def test_no_repo_url_message_asks_the_owner(tmp_path):
    """A workspace that isn't a git repo and has no repo_url must raise an
    actionable ask (Fix 2): name the missing repo_url AND make clear we won't
    silently `git init`. This is the text the owner sees as blocked_on."""
    empty = tmp_path / "fresh"
    empty.mkdir()

    with pytest.raises(WorkspaceError) as ei:
        await prepare_workspace(str(empty), repo_url=None)

    msg = str(ei.value)
    assert "repo_url" in msg
    assert "git init" in msg


@pytest.mark.asyncio
async def test_bad_repo_url_surfaces_git_error(tmp_path):
    """A clone that fails carries the real git stderr through, so blocked_on shows
    the owner *why* (e.g. 'Repository not found') rather than a generic failure."""
    dest = tmp_path / "wont-clone"

    with pytest.raises(WorkspaceError) as ei:
        await prepare_workspace(
            str(dest),
            repo_url="https://github.com/dsdevq/this-repo-does-not-exist-xyz",
        )

    assert "clone failed" in str(ei.value)
