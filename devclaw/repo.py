"""Provision a GitHub repo — so devclaw can take on a *from-scratch* goal.

The goal layer's :func:`workspace.prepare_workspace` deliberately refuses to
``git init`` on its own and requires a ``repo_url`` to clone; :mod:`delivery`
needs an ``origin`` remote to push + open a PR. A build-from-scratch project has
neither until someone creates the repo. This module is that someone: it creates a
GitHub repo via ``gh`` (already installed + authed in the devclaw-mcp image, with
git's credential helper wired to ``gh auth git-credential``) and returns a clone
URL the goal can use.

``--add-readme`` is the load-bearing flag: it gives the new repo an initial commit
and a default branch (``main``), so the very next ``git clone`` + ``fetch`` +
``checkout`` in prepare_workspace succeeds and delivery can branch a PR against a
real base. Auth here is *repo write access* (the ``gh`` token), separate from the
Claude OAuth pillar (cognition billing) — same split as :mod:`delivery`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

#: GitHub repo names: letters, digits, '.', '_', '-'. We slug the goal/idea into one.
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")


class RepoError(RuntimeError):
    pass


def slug_repo_name(text: str, n: int = 60) -> str:
    """Turn an idea/goal-id into a valid GitHub repo name (no spaces/slashes)."""
    s = _NAME_OK.sub("-", text.strip()).strip("-._")
    return (s[:n].strip("-._") or "devclaw-project")


def _default_owner() -> str | None:
    """Owner for new repos. None → gh uses the authenticated user's account."""
    return os.environ.get("DEVCLAW_GITHUB_OWNER") or None


async def _run(*args: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{args[0]} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def _clone_url(slug: str) -> str | None:
    """Resolve a repo's HTTPS clone URL via gh (None if it can't be read)."""
    rc, out = await _run("gh", "repo", "view", slug, "--json", "url", "-q", ".url")
    if rc == 0 and out.strip().startswith("https://"):
        return out.strip() + ".git"
    return None


async def create_repo(
    name: str,
    *,
    private: bool = True,
    description: str = "",
    owner: str | None = None,
) -> dict:
    """Create a GitHub repo and return ``{created, existed, repo, clone_url}``.

    Idempotent: if the repo already exists it is returned (``existed=True``)
    rather than erroring, so re-running a goal setup is safe. Raises
    :class:`RepoError` only when creation genuinely fails (auth/network/quota).
    """
    safe = slug_repo_name(name)
    owner = owner or _default_owner()
    slug = f"{owner}/{safe}" if owner else safe

    # Already there? Hand back its URL instead of failing the whole goal setup.
    if (existing := await _clone_url(slug)) is not None:
        return {"created": False, "existed": True, "repo": slug, "clone_url": existing}

    args = ["gh", "repo", "create", slug, "--add-readme",
            "--private" if private else "--public"]
    if description:
        args += ["--description", description]
    rc, out = await _run(*args)
    if rc != 0:
        raise RepoError(f"gh repo create failed: {out[-400:]}")

    clone_url = await _clone_url(slug) or _extract_clone_url(out, slug)
    if not clone_url:
        raise RepoError(f"repo created but could not resolve its clone URL: {out[-200:]}")
    return {"created": True, "existed": False, "repo": slug, "clone_url": clone_url}


def _extract_clone_url(text: str, slug: str) -> str | None:
    """Fallback: scrape the repo URL gh prints, else synthesize from the slug."""
    m = re.search(r"https://github\.com/[\w.\-/]+", text)
    if m:
        return m.group(0).rstrip("/") + (".git" if not m.group(0).endswith(".git") else "")
    if "/" in slug:  # owner/name known → safe to synthesize
        return f"https://github.com/{slug}.git"
    return None
