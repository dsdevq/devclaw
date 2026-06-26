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


# ---- goal-branch mode (Pillar 2) ------------------------------------------
#
# Spin up a bare local "origin" + a working clone of it so we can exercise
# the real prepare_workspace against a real git tree — no network, no GitHub.

import subprocess


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _git_out(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_origin_with_main(tmp_path):
    """A bare repo on disk with a non-empty ``main`` branch. Returns its path."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed commit")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    return str(origin)


@pytest.mark.asyncio
async def test_no_branch_arg_keeps_legacy_default_branch_reset(tmp_path):
    """Backwards-compat: prepare_workspace called WITHOUT branch must still
    return to the default branch + reset to its tip — the legacy mode the
    goal layer relies on for discovery + done-gate stays intact."""
    origin = _make_origin_with_main(tmp_path)
    ws = tmp_path / "ws"

    out = await prepare_workspace(str(ws), repo_url=origin)
    assert out == "main"
    assert _git_out(ws, "rev-parse", "--abbrev-ref", "HEAD") == "main"


@pytest.mark.asyncio
async def test_goal_branch_created_from_main_when_missing(tmp_path):
    """First item of a checklist goal: ``goal/<id>`` doesn't exist anywhere
    → prepare_workspace creates it locally off the default branch."""
    origin = _make_origin_with_main(tmp_path)
    ws = tmp_path / "ws"

    out = await prepare_workspace(str(ws), repo_url=origin, branch="goal/my-goal")
    assert out == "goal/my-goal"
    assert _git_out(ws, "rev-parse", "--abbrev-ref", "HEAD") == "goal/my-goal"
    assert (ws / "README.md").read_text() == "seed\n"


@pytest.mark.asyncio
async def test_goal_branch_resets_to_remote_tip_when_already_exists(tmp_path):
    """Second + Nth item: branch ``goal/<id>`` already exists on origin (a
    prior item pushed). prepare_workspace fetches + resets to
    ``origin/goal/<id>`` so the workspace sees ALL prior items' commits."""
    origin = _make_origin_with_main(tmp_path)

    pre = tmp_path / "pre"
    subprocess.run(["git", "clone", origin, str(pre)], check=True, capture_output=True)
    _git(pre, "checkout", "-b", "goal/my-goal")
    (pre / "item-1.txt").write_text("first item")
    _git(pre, "add", "-A"); _git(pre, "commit", "-m", "scaffold")
    (pre / "item-2.txt").write_text("second item")
    _git(pre, "add", "-A"); _git(pre, "commit", "-m", "abstractions")
    _git(pre, "push", "-u", "origin", "goal/my-goal")

    ws = tmp_path / "ws"
    out = await prepare_workspace(str(ws), repo_url=origin, branch="goal/my-goal")
    assert out == "goal/my-goal"
    assert _git_out(ws, "rev-parse", "--abbrev-ref", "HEAD") == "goal/my-goal"
    assert (ws / "item-1.txt").read_text() == "first item"
    assert (ws / "item-2.txt").read_text() == "second item"


@pytest.mark.asyncio
async def test_goal_branch_resets_local_changes_to_remote(tmp_path):
    """Pillar 2 invariant: workspace state from a prior task (local-only
    commits + uncommitted debris) must NOT survive into the next item's
    prep — only what's on ``origin/goal/<id>`` counts."""
    origin = _make_origin_with_main(tmp_path)
    ws = tmp_path / "ws"

    await prepare_workspace(str(ws), repo_url=origin, branch="goal/g")
    (ws / "shipped.txt").write_text("would have shipped")
    _git(ws, "add", "-A"); _git(ws, "commit", "-m", "fake shipped")
    (ws / "junk.txt").write_text("uncommitted debris")

    push_helper = tmp_path / "push"
    subprocess.run(
        ["git", "clone", "-b", "main", origin, str(push_helper)],
        check=True, capture_output=True,
    )
    _git(push_helper, "checkout", "-b", "goal/g")
    (push_helper / "shipped.txt").write_text("would have shipped")
    _git(push_helper, "add", "-A"); _git(push_helper, "commit", "-m", "real shipped")
    _git(push_helper, "push", "-u", "origin", "goal/g")

    await prepare_workspace(str(ws), repo_url=origin, branch="goal/g")
    assert (ws / "shipped.txt").read_text() == "would have shipped"
    assert not (ws / "junk.txt").exists()


@pytest.mark.asyncio
async def test_branch_equals_default_branch_uses_legacy_path(tmp_path):
    """Backwards-compat edge: explicitly passing the default branch name
    behaves like the legacy no-branch path — same hard-reset to origin/main."""
    origin = _make_origin_with_main(tmp_path)
    ws = tmp_path / "ws"
    out = await prepare_workspace(str(ws), repo_url=origin, branch="main")
    assert out == "main"
    assert _git_out(ws, "rev-parse", "--abbrev-ref", "HEAD") == "main"
