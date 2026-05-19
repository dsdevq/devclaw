"""Tests for the devclaw-mcp server.

Covers the SSH-shelling tool implementations and the MCP-SDK handshake
(start server, list tools, call devclaw_status against a mocked SSH).
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

from orchestrator import mcp_server

# ─── devclaw_intake ──────────────────────────────────────────────────────────


def test_intake_shells_out_via_ssh_and_parses_json():
    fake_json = {
        "task_id": "2026-05-19-tid",
        "spec_path": "/home/lifekit/.life/tasks/2026-05-19-tid/spec.yaml",
        "budget_min": 30,
        "target_repo": "dsdevq/devclaw",
        "state": "new",
    }
    captured: dict = {}

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None):
        captured["argv"] = argv
        captured["stdin"] = input
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(fake_json) + "\n", stderr=""
        )

    with mock.patch.object(subprocess, "run", side_effect=fake_run), \
         mock.patch.dict(
             "os.environ",
             {"DEVCLAW_VPS_HOST": "vps.example", "DEVCLAW_VPS_USER": "lifekit"},
             clear=False,
         ):
        out = mcp_server.devclaw_intake("file a task", from_surface="pc-kit")

    assert out == fake_json
    argv = captured["argv"]
    assert "lifekit@vps.example" in argv
    # prose was piped on stdin (not embedded in the command line)
    assert captured["stdin"] == "file a task"
    # from-surface flag is in the remote command
    joined_remote = " ".join(argv[argv.index("--") + 1 :])
    assert "devclaw-orchestrator intake" in joined_remote
    assert "--from pc-kit" in joined_remote


def test_intake_returns_error_on_ssh_failure():
    def boom(*_a, **_kw):
        raise OSError("ssh: connect: no route to host")

    with mock.patch.object(subprocess, "run", side_effect=boom):
        out = mcp_server.devclaw_intake("x", from_surface="pc-kit")

    assert out["error"] == "ssh_failed_intake"
    assert "no route to host" in out["detail"]


def test_intake_returns_error_on_timeout():
    def boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)

    with mock.patch.object(subprocess, "run", side_effect=boom):
        out = mcp_server.devclaw_intake("x", from_surface="pc-kit")
    assert out["error"] == "ssh_failed_intake"


def test_intake_returns_error_on_nonzero_rc():
    def fake_run(argv, input=None, **_kw):
        return subprocess.CompletedProcess(argv, returncode=2, stdout="", stderr="boom")

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        out = mcp_server.devclaw_intake("x", from_surface="pc-kit")
    assert out["error"] == "intake_failed"
    assert "boom" in out["detail"]


def test_intake_returns_error_on_bad_json():
    def fake_run(argv, input=None, **_kw):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="not json\n", stderr="")

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        out = mcp_server.devclaw_intake("x", from_surface="pc-kit")
    assert out["error"] == "intake_bad_json"


# ─── devclaw_status ──────────────────────────────────────────────────────────


def test_status_shells_out_and_parses():
    fake = {
        "task_id": "tid",
        "state": "done",
        "last_action": "done",
        "pr_url": "https://github.com/x/y/pull/1",
        "completed_at": "2026-05-19T10:00:00Z",
    }
    captured: dict = {}

    def fake_run(argv, input=None, **_kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(fake), stderr="")

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        out = mcp_server.devclaw_status("tid")
    assert out == fake
    joined = " ".join(captured["argv"])
    assert "devclaw-orchestrator status" in joined
    assert "'tid'" in joined or " tid" in joined


def test_status_survives_failed_call_with_clean_error():
    def boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)

    with mock.patch.object(subprocess, "run", side_effect=boom):
        out = mcp_server.devclaw_status("tid")
    assert out["error"] == "ssh_failed_status"

    # And the server is still importable / build-able after the failure:
    server = mcp_server.build_server()
    assert server is not None


# ─── MCP SDK handshake ──────────────────────────────────────────────────────


def test_build_server_registers_both_tools():
    """Self-test handshake: start the server in-process, list its tools, and
    confirm both names are present with the documented description quirks
    (especially the `from_surface` note for the intake tool)."""
    import asyncio

    server = mcp_server.build_server()

    async def _list():
        return await server.list_tools()

    tools = asyncio.run(_list())
    names = {t.name for t in tools}
    assert names == {"devclaw_intake", "devclaw_status"}, names
    intake_tool = next(t for t in tools if t.name == "devclaw_intake")
    # critical: the kwarg is `from_surface`, NOT `from`
    assert "from_surface" in intake_tool.description
    # the schema must expose `from_surface` as a parameter
    schema = intake_tool.inputSchema
    assert "from_surface" in schema.get("properties", {})


def test_call_status_tool_via_mcp_sdk_handshake():
    """End-to-end: call the registered `devclaw_status` tool through the
    FastMCP server's tool-call surface, with SSH mocked."""
    import asyncio

    server = mcp_server.build_server()
    fake = {"task_id": "tid", "state": "done", "last_action": "done"}

    def fake_run(argv, input=None, **_kw):
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(fake), stderr="")

    async def _call():
        return await server.call_tool("devclaw_status", {"task_id": "tid"})

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        result = asyncio.run(_call())

    # FastMCP returns (content_blocks, structured_result) — the dict comes back
    # in one of those slots. Look for it.
    flattened = json.dumps(result, default=str)
    assert "done" in flattened
    assert "tid" in flattened
