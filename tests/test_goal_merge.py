"""Auto-merge is best-effort and guarded — an empty url is a no-op (never shells
out to gh), so a delivery with no PR can't trigger a merge attempt."""

from __future__ import annotations

import pytest

from devclaw.goal.merge import merge_pr, resolve_automerge
from devclaw.project_registry import ProjectRegistry


@pytest.mark.asyncio
async def test_empty_url_is_a_noop():
    assert await merge_pr("") is False


# ---- resolve_automerge: project override wins over the global default -----


def test_no_registry_falls_back_to_global_default(monkeypatch):
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", False)
    assert resolve_automerge(None, "/src/anything") is False
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", True)
    assert resolve_automerge(None, "/src/anything") is True


def test_unregistered_workspace_falls_back_to_global_default(tmp_path, monkeypatch):
    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", True)
    assert resolve_automerge(reg, "/src/not-a-project") is True


def test_project_override_wins_over_global_default(tmp_path, monkeypatch):
    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/src/p", automerge=False)
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", True)
    # global says on, but this project pins off — override wins.
    assert resolve_automerge(reg, "/src/p") is False


def test_project_with_no_override_inherits_global_default(tmp_path, monkeypatch):
    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/src/p")  # automerge=None
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", True)
    assert resolve_automerge(reg, "/src/p") is True
    monkeypatch.setattr("devclaw.goal.merge.AUTOMERGE_ENABLED", False)
    assert resolve_automerge(reg, "/src/p") is False


# ---- resolve_merge_strategy + strategy-bound merger -------------------------


def test_merge_strategy_project_override_wins(tmp_path, monkeypatch):
    from devclaw.goal.merge import resolve_merge_strategy

    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/src/p", merge_strategy="rebase")
    monkeypatch.setattr("devclaw.goal.merge.DEFAULT_MERGE_STRATEGY", "squash")
    assert resolve_merge_strategy(reg, "/src/p") == "rebase"


def test_merge_strategy_unpinned_and_no_registry_use_default(tmp_path, monkeypatch):
    from devclaw.goal.merge import resolve_merge_strategy

    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/src/p")  # unpinned
    monkeypatch.setattr("devclaw.goal.merge.DEFAULT_MERGE_STRATEGY", "merge")
    assert resolve_merge_strategy(reg, "/src/p") == "merge"
    assert resolve_merge_strategy(None, "/src/p") == "merge"


def test_merge_strategy_invalid_pin_falls_back_to_default(tmp_path, monkeypatch):
    from devclaw.goal.merge import resolve_merge_strategy

    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/src/p", merge_strategy="bogus")
    monkeypatch.setattr("devclaw.goal.merge.DEFAULT_MERGE_STRATEGY", "squash")
    assert resolve_merge_strategy(reg, "/src/p") == "squash"


# ---- GitHub-native auto-merge (--auto) + devclaw's own gate status ----------

def _fake_gh(script):
    """Fake asyncio.create_subprocess_exec. ``script(argv) -> (rc, stdout_bytes)``
    drives each call's result; every call's argv is recorded for assertions."""
    calls: list = []

    class _Proc:
        def __init__(self, rc, out):
            self.returncode = rc
            self._out = out

        async def communicate(self):
            return (self._out, b"")

    async def _exec(*argv, **_kw):
        calls.append(argv)
        rc, out = script(argv)
        return _Proc(rc, out)

    return _exec, calls


def _is_status_post(argv) -> bool:
    return any("statuses/" in a for a in argv) and "context=devclaw/gate" in argv


def _is_auto_merge(argv) -> bool:
    return "merge" in argv and "--auto" in argv


def _is_direct_merge(argv) -> bool:
    return "merge" in argv and "--auto" not in argv


@pytest.mark.asyncio
async def test_merge_pr_prefers_auto_merge_and_posts_gate_status(monkeypatch):
    """The happy path: devclaw posts its own gate as a commit status on the head
    SHA, then merges via GitHub-native --auto (server-side mergeability wait → no
    client race). Proves both the status post and the --auto flag reach gh."""
    def script(argv):
        if "view" in argv:
            return 0, b"deadbeefsha"          # head SHA lookup
        return 0, b""                          # status post + --auto merge
    exec_, calls = _fake_gh(script)
    monkeypatch.setattr("asyncio.create_subprocess_exec", exec_)

    from devclaw.goal.merge import merge_pr
    assert await merge_pr("https://github.com/o/r/pull/7") is True
    # gate status posted on the fetched SHA with the required context
    assert any(_is_status_post(a) and "repos/o/r/statuses/deadbeefsha" in a for a in calls)
    # and the merge used --auto (not an immediate blind merge)
    assert any(_is_auto_merge(a) for a in calls)


@pytest.mark.asyncio
async def test_merge_pr_falls_back_to_direct_when_auto_declined(monkeypatch):
    """A repo without GitHub auto-merge enabled makes --auto error; merge_pr must
    fall back to a direct merge so nothing regresses without the repo config."""
    def script(argv):
        if "view" in argv:
            return 0, b"sha1"
        if _is_auto_merge(argv):
            return 1, b"Auto-merge is not allowed for this repository"
        return 0, b""                          # direct merge succeeds
    exec_, calls = _fake_gh(script)
    monkeypatch.setattr("asyncio.create_subprocess_exec", exec_)

    from devclaw.goal.merge import merge_pr
    assert await merge_pr("https://github.com/o/r/pull/8") is True
    assert any(_is_auto_merge(a) for a in calls)     # tried --auto first
    assert any(_is_direct_merge(a) for a in calls)   # then fell back to direct


@pytest.mark.asyncio
async def test_merge_pr_false_when_both_auto_and_direct_fail(monkeypatch):
    """If --auto is declined AND the direct fallback also fails, merge_pr returns
    False (the caller leaves the PR open and pings the owner) — fail-closed."""
    def script(argv):
        if "view" in argv:
            return 0, b"sha2"
        if "merge" in argv:
            return 1, b"not mergeable"
        return 0, b""
    exec_, _calls = _fake_gh(script)
    monkeypatch.setattr("asyncio.create_subprocess_exec", exec_)

    from devclaw.goal.merge import merge_pr
    assert await merge_pr("https://github.com/o/r/pull/9") is False


@pytest.mark.asyncio
async def test_merge_pr_skips_status_when_sha_unavailable(monkeypatch):
    """If the head-SHA lookup fails, the status post is skipped (best-effort) and
    the merge still proceeds — a status hiccup never blocks the merge."""
    def script(argv):
        if "view" in argv:
            return 1, b""                       # SHA lookup fails
        return 0, b""
    exec_, calls = _fake_gh(script)
    monkeypatch.setattr("asyncio.create_subprocess_exec", exec_)

    from devclaw.goal.merge import merge_pr
    assert await merge_pr("https://github.com/o/r/pull/10") is True
    assert not any(_is_status_post(a) for a in calls)   # no status posted
    assert any(_is_auto_merge(a) for a in calls)        # merge still happened


@pytest.mark.asyncio
async def test_default_merger_binds_strategy_into_the_gh_flag(monkeypatch):
    """default_merger(strategy) must produce a merger that hands `gh pr merge`
    the matching --<strategy> flag — proves the per-project strategy actually
    reaches the subprocess, not just the resolver."""
    from devclaw.goal.merge import default_merger

    captured = {}

    class _Proc:
        returncode = 0
        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    merger = default_merger("rebase")
    assert await merger("https://github.com/o/r/pull/1") is True
    assert "--rebase" in captured["args"]
    assert "--squash" not in captured["args"]
