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


async def prepare_workspace(workspace_dir: str, repo_url: str | None = None) -> str:
    """Ensure ``workspace_dir`` is a pristine checkout of the repo's default
    branch at latest origin. Clones from ``repo_url`` if the dir isn't a repo.
    Returns the default branch name. Raises :class:`WorkspaceError` on failure.

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

    base = await _default_branch(workspace_dir)
    for cmd in (
        ("git", "checkout", "-f", base),
        ("git", "reset", "--hard", f"origin/{base}"),
        ("git", "clean", "-fdx"),
    ):
        rc, out = await _run(*cmd, cwd=workspace_dir)
        if rc != 0:
            raise WorkspaceError(f"{' '.join(cmd)} failed: {out[-300:]}")
    return base
