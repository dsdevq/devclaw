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
