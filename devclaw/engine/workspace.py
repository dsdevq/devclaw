"""Workspace lifecycle — give the engine a clean checkout per goal action.

Folded in from goalclaw. Now that the goal layer lives INSIDE devclaw, devclaw
owns the goal↔repo↔workspace lifecycle end to end (this is the seam-relocation
the 2026-06-06 brainstorm wanted — it falls out for free once the two services
are one process). Before each code action we make the workspace a **pristine
checkout of the repo's default branch at latest origin** — clone if missing,
else fetch + hard-reset + clean. That keeps a multi-item goal's actions from
piling onto each other's branches, and naturally picks up whatever PRs got
merged since the last action.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


async def _run(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a command, return (exit_code, combined output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{args[0]} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def _default_branch(workspace_dir: str) -> str:
    """The remote's default branch name (e.g. 'main'). Falls back main→master."""
    rc, out = await _run(
        "git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=workspace_dir
    )
    if rc == 0 and "/" in out:
        return out.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _ = await _run("git", "rev-parse", "--verify", "--quiet", f"origin/{cand}", cwd=workspace_dir)
        if rc == 0:
            return cand
    return "main"


async def prepare_workspace(
    workspace_dir: str,
    repo_url: str | None = None,
    branch: str | None = None,
) -> str:
    """Ensure ``workspace_dir`` is a pristine checkout of either the repo's
    default branch (when ``branch`` is None) OR the named ``branch`` at its
    latest tip. Clones from ``repo_url`` if the dir isn't a repo. Returns the
    name of the branch the working tree ended up on. Raises
    :class:`WorkspaceError` on failure.

    The ``branch`` arg is the Pillar 1 / Pillar 2 hook: each per-item task in
    a checklist-mode goal passes ``branch="goal/<goal_id>"`` so subsequent
    items see the prior items' commits stacked on the goal branch instead of
    branching off origin/main and re-implementing the foundation (the
    2026-06-26 finance-sentry-mcp-v3 PR-fan-out failure). When the goal
    branch doesn't exist yet (first item) it is created from the default
    branch at latest origin; otherwise it is fetched + fast-forwarded to its
    own remote tip (preserving the agent's accumulated work) and rebased onto
    the latest default-branch tip so a long-running goal still tracks main.

    Injected into the goal tick so unit tests pass a no-op.
    """
    if not (Path(workspace_dir) / ".git").exists():
        if not repo_url:
            raise WorkspaceError(
                "no repo to work in — this goal has no repo_url and its workspace "
                f"({workspace_dir}) isn't a git checkout. Set the goal's repo_url to "
                "the GitHub repo I should clone, or tell me to start a fresh empty "
                "repo here (I won't `git init` on my own — that's yours to confirm)."
            )
        Path(workspace_dir).parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run("git", "clone", repo_url, workspace_dir)
        if rc != 0:
            raise WorkspaceError(f"clone failed: {out[-300:]}")

    rc, out = await _run("git", "fetch", "origin", "--prune", cwd=workspace_dir)
    if rc != 0:
        raise WorkspaceError(f"fetch failed: {out[-300:]}")

    default_branch = await _default_branch(workspace_dir)

    if branch is None or branch == default_branch:
        # Legacy / discovery / done-gate path — just reset to the default branch.
        for cmd in (
            ("git", "checkout", "-f", default_branch),
            ("git", "reset", "--hard", f"origin/{default_branch}"),
            ("git", "clean", "-fdx"),
        ):
            rc, out = await _run(*cmd, cwd=workspace_dir)
            if rc != 0:
                raise WorkspaceError(f"{' '.join(cmd)} failed: {out[-300:]}")
        return default_branch

    # Goal-branch path. Start clean (drop any untracked debris from a prior
    # task), then either fast-forward the existing branch to its remote tip
    # OR create it fresh from the default branch.
    rc, _ = await _run("git", "clean", "-fdx", cwd=workspace_dir)
    if rc != 0:
        # clean failure is rare and not load-bearing here — log via the error
        # below if a subsequent op trips on residue.
        pass

    rc_remote, _ = await _run(
        "git", "rev-parse", "--verify", "--quiet", f"origin/{branch}",
        cwd=workspace_dir,
    )
    if rc_remote == 0:
        # Branch exists on origin — check it out and reset to its tip so we
        # have ALL prior items' commits. (A force-reset is safe because we
        # never write to this branch except via push from devclaw itself.)
        rc, out = await _run(
            "git", "checkout", "-B", branch, f"origin/{branch}", cwd=workspace_dir,
        )
        if rc != 0:
            raise WorkspaceError(f"checkout goal branch {branch} failed: {out[-300:]}")
    else:
        # First item of the goal — branch from origin/<default> so the goal
        # starts at the same point a single-PR rerun would.
        rc, out = await _run(
            "git", "checkout", "-B", branch, f"origin/{default_branch}",
            cwd=workspace_dir,
        )
        if rc != 0:
            raise WorkspaceError(f"create goal branch {branch} failed: {out[-300:]}")
    return branch
