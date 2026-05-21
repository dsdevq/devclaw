"""Tests for the devclaw-mcp server.

Covers the SSH-shelling tool implementations and the MCP-SDK handshake
(start server, list tools, call devclaw_status against a mocked SSH).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from unittest import mock

import pytest

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
    # remote command is a SINGLE argv element after the host — see the
    # docstring on `_ssh_argv` for why splitting across argv elements is
    # broken. The last element holds the whole remote command string.
    remote_cmd = argv[-1]
    assert "devclaw-orchestrator intake" in remote_cmd
    assert "--from pc-kit" in remote_cmd


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


# ─── Integration: real argv shape via a fake ssh binary ─────────────────────
#
# These tests do NOT mock subprocess. They install a tiny shell script as the
# `ssh` binary, which records its argv to a temp file and then synthesizes a
# JSON response. This is the only test shape that catches the pre-fix bug
# where `_ssh_argv` split the remote command across multiple argv elements:
# what OpenSSH actually does on the wire is glue everything after the host
# together with spaces and feed it to the remote shell as ONE string, so
# `["/bin/sh", "-c", remote_cmd]` collapses to `/bin/sh -c <first-token>`
# remotely and silently drops the rest. The fake-ssh fixture below mirrors
# that joining behavior so the assertion (`one argv slot after the host`)
# exercises the real interaction.


def _install_fake_ssh(tmp_path: Path, response_json: dict) -> tuple[Path, Path]:
    """Write a shell script at tmp_path/ssh that records its argv and prints
    the given JSON. Returns (ssh_path, argv_log_path)."""
    argv_log = tmp_path / "argv.log"
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(response_json))
    ssh_path = tmp_path / "ssh"
    # `printf '%s\n'` writes one line per argv element so the test can
    # reconstruct the exact argv shape — including elements that contain
    # spaces (which would be ambiguous if we joined with spaces).
    ssh_path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            printf '%s\\n' "$#" >> {argv_log}
            for a in "$@"; do
              printf '%s\\n' "$a" >> {argv_log}
            done
            cat {response_path}
            """
        )
    )
    ssh_path.chmod(ssh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return ssh_path, argv_log


def _read_argv(argv_log: Path) -> list[str]:
    lines = argv_log.read_text().splitlines()
    n = int(lines[0])
    return lines[1 : 1 + n]


def _assert_remote_cmd_is_single_argv_slot(
    argv: list[str], host_token: str, remote_cmd_fragments: list[str]
) -> str:
    """Find the host argv slot and assert the remote command lives in a
    SINGLE element after it. Returns that element."""
    assert host_token in argv, f"host token {host_token!r} not in argv {argv!r}"
    host_idx = argv.index(host_token)
    after_host = argv[host_idx + 1 :]
    # The whole point of the fix: exactly one slot carries the remote
    # command. Anything more means we're back to the bug where ssh's
    # space-joining clobbers `sh -c`.
    assert len(after_host) == 1, (
        "remote command must be a SINGLE argv element after the host — "
        f"got {len(after_host)}: {after_host!r}"
    )
    remote_cmd = after_host[0]
    for frag in remote_cmd_fragments:
        assert frag in remote_cmd, f"{frag!r} missing from remote_cmd {remote_cmd!r}"
    return remote_cmd


@pytest.fixture
def fake_ssh_env(tmp_path, monkeypatch):
    """Install a fake ssh and point DEVCLAW_SSH_BIN at it. Returns a small
    helper bundle (install fn + env applied)."""
    monkeypatch.setenv("DEVCLAW_VPS_HOST", "vps.example")
    monkeypatch.setenv("DEVCLAW_VPS_USER", "lifekit")

    def install(response_json: dict) -> Path:
        ssh_path, argv_log = _install_fake_ssh(tmp_path, response_json)
        monkeypatch.setenv("DEVCLAW_SSH_BIN", str(ssh_path))
        return argv_log

    return install


def test_status_argv_delivers_remote_cmd_as_single_arg(fake_ssh_env):
    """Regression: pre-fix `_ssh_argv` split the remote command into
    `["--", "/bin/sh", "-c", remote_cmd]` — four argv elements after the
    host. OpenSSH joins those with spaces remotely, so `sh -c` only ran the
    first token (`/bin/sh`) of the resulting string and the actual command
    arrived as $0/$1/... and was silently lost, producing
    `error: the following arguments are required: cmd` from argparse on the
    remote side. This test runs against a real ssh-shaped subprocess (no
    mock) and asserts the single-argv-slot invariant."""
    fake = {"task_id": "tid-xyz", "state": "ready", "last_action": "intake"}
    argv_log = fake_ssh_env(fake)

    out = mcp_server.devclaw_status("tid-xyz")

    assert out == fake
    argv = _read_argv(argv_log)
    remote_cmd = _assert_remote_cmd_is_single_argv_slot(
        argv,
        host_token="lifekit@vps.example",
        remote_cmd_fragments=["devclaw-orchestrator status", "tid-xyz"],
    )
    # And nothing accidentally re-introduced the `--`/`/bin/sh`/`-c` split.
    assert "--" not in argv[argv.index("lifekit@vps.example") + 1 :]
    assert "/bin/sh" not in remote_cmd.split()[:1]


def test_intake_argv_delivers_remote_cmd_as_single_arg(fake_ssh_env):
    """Same regression check for `devclaw_intake` — the bug lived in the
    shared `_ssh_argv`, so both tools were affected. Also confirms `prose`
    is piped on stdin rather than embedded in argv (the fake ssh script
    doesn't read stdin, but we do verify the argv shape stays clean)."""
    fake = {
        "task_id": "2026-05-20-tid",
        "spec_path": "/home/lifekit/.life/tasks/2026-05-20-tid/spec.yaml",
        "budget_min": 30,
        "target_repo": "dsdevq/devclaw",
        "state": "new",
    }
    argv_log = fake_ssh_env(fake)

    out = mcp_server.devclaw_intake("file a task", from_surface="pc-kit")

    assert out == fake
    argv = _read_argv(argv_log)
    _assert_remote_cmd_is_single_argv_slot(
        argv,
        host_token="lifekit@vps.example",
        remote_cmd_fragments=["devclaw-orchestrator intake", "--from", "pc-kit"],
    )


def test_status_argv_quotes_task_id_with_shell_metacharacters(fake_ssh_env):
    """If a task_id ever contained shell metacharacters, `shlex.quote` in
    the caller should keep them from being interpreted by the remote shell.
    This isn't expected in practice (ids are date-prefixed slugs) but it's
    cheap to lock in."""
    fake = {"task_id": "weird id", "state": "ready"}
    argv_log = fake_ssh_env(fake)

    out = mcp_server.devclaw_status("weird; rm -rf /")
    assert out == fake

    argv = _read_argv(argv_log)
    remote_cmd = argv[-1]
    # the rm -rf substring is present (it's inside the task_id arg) but it
    # must be quoted so the remote shell sees it as data, not a command.
    assert "'weird; rm -rf /'" in remote_cmd or '"weird; rm -rf /"' in remote_cmd
