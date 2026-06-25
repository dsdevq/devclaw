"""repo.create_repo — provisioning a GitHub repo for from-scratch goals.

_run is monkeypatched so no real gh/network is touched; we assert on the gh
argv it builds and on the idempotent/existing-repo branches."""
from __future__ import annotations

import pytest

from devclaw.delivery import repo


def test_slug_repo_name_makes_valid_names():
    assert repo.slug_repo_name("Build a Todo App!") == "Build-a-Todo-App"
    assert repo.slug_repo_name("  spaces  and/slashes  ") == "spaces-and-slashes"
    assert repo.slug_repo_name("") == "devclaw-project"
    assert repo.slug_repo_name("---") == "devclaw-project"


def test_extract_clone_url_scrapes_or_synthesizes():
    assert repo._extract_clone_url("created https://github.com/me/app", "me/app") == (
        "https://github.com/me/app.git"
    )
    # no URL in output but owner/name known → synthesize
    assert repo._extract_clone_url("ok", "me/app") == "https://github.com/me/app.git"
    assert repo._extract_clone_url("ok", "bare") is None


@pytest.mark.asyncio
async def test_create_repo_creates_when_absent(monkeypatch):
    """When the repo doesn't exist, it shells `gh repo create ... --add-readme`
    and returns the resolved clone URL."""
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str):
        calls.append(args)
        if args[:3] == ("gh", "repo", "view"):
            # first view (existence check) misses; later view resolves the URL
            if "--json" in args and any(c[:3] == ("gh", "repo", "create") for c in calls):
                return 0, "https://github.com/dsdevq/todo"
            return 1, "not found"
        if args[:3] == ("gh", "repo", "create"):
            return 0, "✓ Created repository dsdevq/todo on GitHub"
        return 1, "unexpected"

    monkeypatch.setattr(repo, "_run", fake_run)
    monkeypatch.setenv("DEVCLAW_GITHUB_OWNER", "dsdevq")

    out = await repo.create_repo("todo", private=True, description="d")

    assert out["created"] is True and out["existed"] is False
    assert out["repo"] == "dsdevq/todo"
    assert out["clone_url"] == "https://github.com/dsdevq/todo.git"
    create = next(c for c in calls if c[:3] == ("gh", "repo", "create"))
    assert "--add-readme" in create and "--private" in create
    assert "--description" in create


@pytest.mark.asyncio
async def test_create_repo_idempotent_when_exists(monkeypatch):
    """An existing repo is returned (existed=True), never re-created."""
    async def fake_run(*args: str):
        if args[:3] == ("gh", "repo", "view"):
            return 0, "https://github.com/dsdevq/todo"
        raise AssertionError(f"should not run {args}")

    monkeypatch.setattr(repo, "_run", fake_run)
    monkeypatch.setenv("DEVCLAW_GITHUB_OWNER", "dsdevq")

    out = await repo.create_repo("todo")

    assert out["existed"] is True and out["created"] is False
    assert out["clone_url"] == "https://github.com/dsdevq/todo.git"


@pytest.mark.asyncio
async def test_create_repo_raises_on_failure(monkeypatch):
    async def fake_run(*args: str):
        if args[:3] == ("gh", "repo", "view"):
            return 1, "not found"
        if args[:3] == ("gh", "repo", "create"):
            return 1, "HTTP 403: name already exists / no permission"
        return 1, "x"

    monkeypatch.setattr(repo, "_run", fake_run)
    monkeypatch.delenv("DEVCLAW_GITHUB_OWNER", raising=False)

    with pytest.raises(repo.RepoError):
        await repo.create_repo("todo")
