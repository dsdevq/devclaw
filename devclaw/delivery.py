"""Deliver a completed task's change as a reviewable branch + PR.

After a task settles ``done`` (the verify gate passed), the agent's change is
sitting **uncommitted** in the workspace. Delivery turns that into something you
*review* instead of *produce*: a branch, a commit, a push, and — if the remote
is GitHub and ``gh`` is authed — a pull request whose URL is recorded on the task.

Design:
  * **Best-effort + non-fatal.** A delivery failure never un-does a ``done`` task;
    it records what it managed (``branch`` / ``pushed`` / ``pr_url`` / ``error``).
  * **Graceful degradation.** Not a git repo, or no changes, or no remote, or no
    auth → it does as much as it can (often: commit to a local branch) and stops.
  * **Auth** is a GitHub token (``GITHUB_TOKEN`` / ``GH_TOKEN``) or ``gh``'s own
    login — this is *repo push access*, separate from the Claude OAuth pillar
    (which is about cognition billing, not git).
"""

from __future__ import annotations

import asyncio
import os
import re


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "change"


def _commit_title(goal: str, limit: int = 72) -> str:
    first = goal.strip().splitlines()[0] if goal.strip() else "devclaw change"
    return first[:limit].rstrip()


async def _run(prog: str, *args: str, cwd: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined-output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            prog, *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{prog} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


def _extract_pr_url(text: str) -> str | None:
    m = re.search(r"https://github\.com/\S+/pull/\d+", text)
    return m.group(0) if m else None


async def deliver_change(*, workspace_dir: str, task_id: str, goal: str) -> dict:
    """Commit the workspace's change to a branch and (best-effort) push + open a PR.
    Returns a verdict dict; never raises."""
    result: dict = {"delivered": False, "branch": None, "committed": False,
                    "pushed": False, "pr_url": None, "error": None}

    rc, _ = await _run("git", "rev-parse", "--is-inside-work-tree", cwd=workspace_dir)
    if rc != 0:
        result["error"] = "workspace is not a git repository"
        return result

    rc, status = await _run("git", "status", "--porcelain", cwd=workspace_dir)
    if rc == 0 and not status.strip():
        result["error"] = "no changes to deliver"
        return result

    branch = f"devclaw/{task_id[:8]}-{_slug(goal)}"
    result["branch"] = branch

    rc, out = await _run("git", "checkout", "-b", branch, cwd=workspace_dir)
    if rc != 0:
        result["error"] = f"branch failed: {out}"
        return result
    await _run("git", "add", "-A", cwd=workspace_dir)
    msg = f"{_commit_title(goal)}\n\nDelivered by devclaw (task {task_id})."
    rc, out = await _run(
        "git", "-c", "user.email=devclaw@local", "-c", "user.name=devclaw",
        "commit", "-m", msg, cwd=workspace_dir,
    )
    if rc != 0:
        result["error"] = f"commit failed: {out}"
        return result
    result["committed"] = True

    # Push only if there's a remote. (Local-only repos — e.g. clones of a local
    # path — have no GitHub remote; we stop at the local commit, which is still
    # a reviewable artifact.)
    rc, remote = await _run("git", "remote", "get-url", "origin", cwd=workspace_dir)
    if rc != 0 or not remote.strip():
        result["error"] = "no 'origin' remote — left change on a local branch"
        result["delivered"] = True  # a local branch is still a reviewable result
        return result

    rc, out = await _run("git", "push", "-u", "origin", branch, cwd=workspace_dir)
    if rc != 0:
        result["error"] = f"push failed (check repo push auth): {out[-300:]}"
        result["delivered"] = True  # committed locally; push is what failed
        return result
    result["pushed"] = True

    # Open a PR only on a GitHub remote with gh available/authed.
    if "github.com" in remote:
        rc, out = await _run(
            "gh", "pr", "create", "--head", branch,
            "--title", _commit_title(goal),
            "--body", f"Delivered by devclaw (task `{task_id}`). Verified by the task's gate.",
            cwd=workspace_dir,
        )
        url = _extract_pr_url(out)
        if url:
            result["pr_url"] = url
        elif rc != 0:
            result["error"] = f"pushed, but gh pr create failed: {out[-300:]}"

    result["delivered"] = True
    return result
