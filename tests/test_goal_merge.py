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
