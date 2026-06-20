"""preview hosting — arg building, resource caps, eviction, start happy-path.

_run is monkeypatched so no docker is touched; asyncio.sleep is neutralized so
the readiness loop doesn't actually wait."""
from __future__ import annotations

import pytest

from devclaw import preview


def test_preview_name_slugifies():
    assert preview.preview_name("todo-fullstack") == "devclaw-preview-todo-fullstack"
    assert preview.preview_name("My App!") == "devclaw-preview-My-App"
    assert preview.preview_name("///") == "devclaw-preview-app"


def test_build_run_args_has_resource_caps_and_ports():
    args = preview._build_run_args(name="devclaw-preview-x", host_path="/srv/ws/x", port=8000)
    j = " ".join(args)
    assert "-d" in args and "--name" in args
    assert "--memory" in args and preview.PREVIEW_MEMORY in args  # hard cap
    assert "--cpus" in args
    assert "127.0.0.1:8000:8000" in j  # loopback-only publish
    assert "/srv/ws/x:/app" in j
    assert args[-1] == preview._LAUNCHER and "--entrypoint" in args


@pytest.fixture()
def _no_sleep(monkeypatch):
    async def _fast(_):
        return None
    monkeypatch.setattr(preview.asyncio, "sleep", _fast)


@pytest.mark.asyncio
async def test_start_preview_happy_path(monkeypatch, _no_sleep):
    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "ps":
            return 0, ""              # no existing previews → no eviction
        if args[0] == "run":
            return 0, "deadbeef"
        if args[0] == "inspect":
            return 0, "true"          # container running
        if args[0] == "exec":
            return 0, ""              # ready
        return 0, ""                  # rm, etc.

    monkeypatch.setattr(preview, "_run", fake_run)
    monkeypatch.setattr(preview, "_translate_workspace_path", lambda p: "/srv/ws/todo")

    out = await preview.start_preview("/var/lib/devclaw/workspaces/todo", "todo")

    assert out["container"] == "devclaw-preview-todo"
    assert out["ready"] is True
    assert out["evicted"] == []
    assert out["api_docs_url"].endswith(":8000/docs")
    assert out["frontend_url"].endswith(":8000/")
    assert any(c[0] == "run" for c in calls)


@pytest.mark.asyncio
async def test_start_preview_evicts_oldest_over_cap(monkeypatch, _no_sleep):
    monkeypatch.setattr(preview, "PREVIEW_MAX", 2)
    removed = []

    async def fake_run(*args):
        if args[0] == "ps":
            # docker ps lists newest-first; 3 existing previews
            return 0, "devclaw-preview-c\ndevclaw-preview-b\ndevclaw-preview-a"
        if args[0] == "rm":
            removed.append(args[-1])
            return 0, ""
        if args[0] == "run":
            return 0, "id"
        if args[0] == "inspect":
            return 0, "true"
        if args[0] == "exec":
            return 0, ""
        return 0, ""

    monkeypatch.setattr(preview, "_run", fake_run)
    monkeypatch.setattr(preview, "_translate_workspace_path", lambda p: "/srv/ws/new")

    out = await preview.start_preview("/ws/new", "new")

    # starting 'new' with 3 existing + cap 2 → must evict oldest-first until
    # (remaining others)+1 <= 2, i.e. evict a and b, keep c.
    assert "devclaw-preview-a" in out["evicted"]
    assert "devclaw-preview-b" in out["evicted"]
    assert "devclaw-preview-c" not in out["evicted"]


@pytest.mark.asyncio
async def test_start_preview_raises_if_container_exits(monkeypatch, _no_sleep):
    async def fake_run(*args):
        if args[0] == "ps":
            return 0, ""
        if args[0] == "run":
            return 0, "id"
        if args[0] == "inspect":
            return 0, "false"         # exited during startup
        if args[0] == "logs":
            return 0, "ModuleNotFoundError: No module named 'fastapi'"
        return 0, ""

    monkeypatch.setattr(preview, "_run", fake_run)
    monkeypatch.setattr(preview, "_translate_workspace_path", lambda p: "/srv/ws/x")

    with pytest.raises(preview.PreviewError):
        await preview.start_preview("/ws/x", "x")
