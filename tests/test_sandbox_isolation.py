"""Sandbox config-isolation tests — the curated ~/.claude boundary.

The per-task sandbox must NOT project the whole host ~/.claude into the engineer
(that would leak personal skills/, plugins/ + their MCP servers, the global
CLAUDE.md that points at the unmounted ~/memory, and projects/ history —
non-reproducible and full of tools that fail or mislead). It mounts only a curated
allowlist, default just the OAuth credential. These pin the mount posture so the
leak can't silently come back, and assert it stays unit-testable (pure arg build,
no docker).
"""

import pytest

from devclaw.engine import EngineRequest
from devclaw.engine import sandcastle as sc


CLAUDE_DIR = "/home/me/.claude"


def _claude_mount_targets(args: list[str]) -> list[str]:
    """The container-side path of every `-v …:ro` mount under the config dir."""
    targets = []
    for i, a in enumerate(args):
        if a == "-v" and i + 1 < len(args):
            spec = args[i + 1]
            if sc.CONTAINER_CLAUDE_DIR in spec:
                targets.append(spec)
    return targets


# ---- the shipped default allowlist (intent + regression guard) ----


def test_default_allowlist_is_the_oauth_identity_pair():
    # Auth identity in, everything else out. Both files are needed: the credential
    # is the token; `.claude.json` is the account identity the ACP agentic loop
    # needs to act (credential-only made the agent hang — a live-found regression).
    assert sc.SANDBOX_CLAUDE_ALLOWLIST == (".credentials.json", ".claude.json")


# ---- claude mounts (pure) ----


def test_claude_mounts_only_allowlisted_entries_read_only():
    mounts = sc._build_claude_mounts(CLAUDE_DIR, (".credentials.json",))
    assert mounts == [
        "-v",
        f"{CLAUDE_DIR}/.credentials.json:{sc.CONTAINER_CLAUDE_DIR}/.credentials.json:ro",
    ]


def test_claude_mounts_never_bind_the_whole_dir():
    # The leak we're closing: a single mount of the entire host ~/.claude.
    mounts = sc._build_claude_mounts(CLAUDE_DIR, sc.SANDBOX_CLAUDE_ALLOWLIST)
    whole_dir = f"{CLAUDE_DIR}:{sc.CONTAINER_CLAUDE_DIR}:ro"
    assert whole_dir not in mounts


def test_claude_mounts_honor_a_wider_allowlist():
    mounts = sc._build_claude_mounts(CLAUDE_DIR, (".credentials.json", "skills"))
    assert "-v" in mounts
    assert f"{CLAUDE_DIR}/skills:{sc.CONTAINER_CLAUDE_DIR}/skills:ro" in mounts
    # still no whole-dir projection
    assert f"{CLAUDE_DIR}:{sc.CONTAINER_CLAUDE_DIR}:ro" not in mounts


def test_claude_mounts_tolerate_slashy_entries():
    mounts = sc._build_claude_mounts(CLAUDE_DIR + "/", ("/.credentials.json/",))
    assert mounts == [
        "-v",
        f"{CLAUDE_DIR}/.credentials.json:{sc.CONTAINER_CLAUDE_DIR}/.credentials.json:ro",
    ]


# ---- full docker argv (pure) ----


def test_docker_args_posture():
    args = sc._build_docker_args(
        container_name="devclaw-deadbeef",
        host_bind_path="/host/ws",
        claude_dir=CLAUDE_DIR,
        payload='{"kind":"implement_feature"}',
    )
    # ephemeral + named + labeled for the startup orphan sweep (--rm dies with
    # the docker CLI, so a crash mid-task leaves the container with no reaper;
    # the label is the durable handle sweep_orphan_sandboxes matches on)
    assert args[:2] == ["run", "--rm"]
    assert "--name" in args and args[args.index("--name") + 1] == "devclaw-deadbeef"
    assert "--label" in args and args[args.index("--label") + 1] == "devclaw.sandbox=1"
    # never the deploy label — deploy containers are outside the sweep's scope
    assert "devclaw.deploy=1" not in args
    # workspace bound
    assert f"/host/ws:{sc.CONTAINER_WORKSPACE}" in args
    # the ONLY config mounts are the auth identity pair — no whole-dir leak
    assert _claude_mount_targets(args) == [
        f"{CLAUDE_DIR}/.credentials.json:{sc.CONTAINER_CLAUDE_DIR}/.credentials.json:ro",
        f"{CLAUDE_DIR}/.claude.json:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro",
    ]
    # writable scratch overlays survive the curation
    assert f"{sc.CONTAINER_CLAUDE_DIR}/session-env:rw,exec" in args
    assert f"{sc.CONTAINER_CLAUDE_DIR}/shell-snapshots:rw,exec" in args
    # image + payload land last, payload terminal
    assert args[-2] == sc.SANDBOX_IMAGE
    assert args[-1] == '{"kind":"implement_feature"}'


def test_docker_args_do_not_leak_skills_or_plugins():
    args = sc._build_docker_args(
        container_name="c",
        host_bind_path="/host/ws",
        claude_dir=CLAUDE_DIR,
        payload="{}",
    )
    joined = " ".join(args)
    for leaked in ("/skills", "/plugins", "/projects", "/CLAUDE.md", "/history.jsonl"):
        assert leaked not in joined


# ---- workspace pre-flight (close the silent-timeout traps) ----


@pytest.fixture
def in_prefix(monkeypatch):
    """Simulate a containerized devclaw where workspaces live under
    /var/lib/devclaw/workspaces (container view) ↔ /srv/devclaw/workspaces (host)."""
    monkeypatch.setenv("DEVCLAW_CONTAINER_PATH_PREFIX", "/var/lib/devclaw/workspaces")
    monkeypatch.setenv("DEVCLAW_HOST_PATH_PREFIX", "/srv/devclaw/workspaces")


@pytest.fixture
def no_prefix(monkeypatch):
    """Local-dev: devclaw runs directly on the host, no translation."""
    monkeypatch.delenv("DEVCLAW_CONTAINER_PATH_PREFIX", raising=False)
    monkeypatch.delenv("DEVCLAW_HOST_PATH_PREFIX", raising=False)


def test_validate_workspace_rejects_out_of_prefix_path(in_prefix, tmp_path):
    # The 2026-06-25 finance-sentry-mcp incident: openclaw passed an in-its-own-
    # container path that wasn't under devclaw's prefix; docker mounted an empty
    # host dir at /workspace and every task timed out at 1800s with no signal.
    err = sc._validate_workspace("/home/node/.openclaw/agents/devclaw/tmp/foo")
    assert err is not None
    assert "outside the devclaw workspaces mount" in err
    assert "/var/lib/devclaw/workspaces" in err


def test_validate_workspace_rejects_missing_path(in_prefix):
    err = sc._validate_workspace("/var/lib/devclaw/workspaces/never-cloned")
    assert err is not None
    assert "does not exist" in err


def test_validate_workspace_rejects_empty_dir(in_prefix, tmp_path, monkeypatch):
    # An empty bind-source produces a silent timeout — this is the exact failure
    # mode of the finance-sentry-mcp incident's host bind path.
    monkeypatch.setenv("DEVCLAW_CONTAINER_PATH_PREFIX", str(tmp_path))
    empty = tmp_path / "empty-ws"
    empty.mkdir()
    err = sc._validate_workspace(str(empty))
    assert err is not None
    assert "EMPTY directory" in err


def test_validate_workspace_accepts_populated_dir(in_prefix, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCLAW_CONTAINER_PATH_PREFIX", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("hi")
    assert sc._validate_workspace(str(ws)) is None


def test_validate_workspace_passes_through_in_local_dev(no_prefix, tmp_path):
    # No prefix env → any populated, existing path is fine (the local-dev posture
    # where devclaw and docker share the same filesystem view).
    (tmp_path / "f").write_text("x")
    assert sc._validate_workspace(str(tmp_path)) is None


def test_validate_workspace_still_rejects_missing_in_local_dev(no_prefix, tmp_path):
    # Even without a prefix, a non-existent workspace mounts as nothing and
    # times out — keep the precheck so the operator gets a clear message.
    err = sc._validate_workspace(str(tmp_path / "nope"))
    assert err is not None and "does not exist" in err


# ---- API-key refusal (belt + suspenders, alongside the runner's own check) ----


def test_strip_api_keys_removes_both_vars():
    clean = sc._strip_api_keys(
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "PATH": "/bin"}
    )
    assert "ANTHROPIC_API_KEY" not in clean
    assert "ANTHROPIC_AUTH_TOKEN" not in clean
    assert clean["PATH"] == "/bin"  # unrelated env preserved


# ---- workspace-trust mount override (fix/claude-workspace-trust) ----
# The in-sandbox claude dead-stopped on the untrusted-workspace guard because
# the bound .claude.json (keyed by host paths) never trusted /workspace. The
# fix binds a PRE-TRUSTED COPY for the .claude.json entry only; everything else
# stays the raw read-only bind. These pin that seam.


def test_claude_json_binds_the_trusted_copy_when_provided():
    mounts = sc._build_claude_mounts(
        CLAUDE_DIR,
        (".credentials.json", ".claude.json"),
        claude_json_src="/tmp/devclaw-claude-XYZ.json",
    )
    # .credentials.json still binds straight from the host dir, read-only...
    assert (
        f"{CLAUDE_DIR}/.credentials.json:{sc.CONTAINER_CLAUDE_DIR}/.credentials.json:ro"
        in mounts
    )
    # ...but .claude.json binds the trusted copy at the SAME container target.
    assert (
        f"/tmp/devclaw-claude-XYZ.json:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro"
        in mounts
    )
    # The raw host .claude.json is NOT bound when an override is present.
    assert (
        f"{CLAUDE_DIR}/.claude.json:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro"
        not in mounts
    )


def test_claude_json_falls_back_to_raw_bind_without_override():
    # None override (host config unreadable) → pre-trust behavior: raw bind.
    mounts = sc._build_claude_mounts(CLAUDE_DIR, (".claude.json",), claude_json_src=None)
    assert mounts == [
        "-v",
        f"{CLAUDE_DIR}/.claude.json:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro",
    ]


def test_build_docker_args_threads_the_trusted_copy():
    args = sc._build_docker_args(
        container_name="devclaw-test",
        host_bind_path="/host/ws",
        claude_dir=CLAUDE_DIR,
        payload="{}",
        claude_json_src="/tmp/devclaw-claude-XYZ.json",
    )
    assert (
        f"/tmp/devclaw-claude-XYZ.json:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro"
        in args
    )


async def test_run_sandcastle_binds_trusted_copy_and_unlinks_it(no_prefix, tmp_path, monkeypatch):
    """The integration wiring the pure mount tests can't see: run_sandcastle
    computes the pre-trusted .claude.json copy, binds it into the container as
    the .claude.json source, and deletes it in its outer `finally` once the
    container has exited. A dropped `claude_json_src=` kwarg or a missing unlink
    would keep the whole suite green — this pins the full path."""
    (tmp_path / "f").write_text("x")  # a populated, existing workspace passes validation
    fake_copy = tmp_path / "devclaw-claude-fake.json"
    fake_copy.write_text("{}")

    seen: dict = {}

    def fake_write_trusted_copy(src, workspace_path):
        seen["copy_args"] = (src, workspace_path)
        return str(fake_copy)

    monkeypatch.setattr(sc, "write_trusted_copy", fake_write_trusted_copy)

    class _FakeProc:
        returncode = 0  # non-None → teardown is correctly skipped on clean exit

    async def fake_exec(_bin, *args, **kwargs):
        seen["docker_args"] = args
        return _FakeProc()

    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", fake_exec)

    async def fake_consume(proc, on_event, label):
        return {"status": "ok"}

    monkeypatch.setattr(sc, "consume_runner_output", fake_consume)

    req = EngineRequest(kind="implement_feature", workspace_dir=str(tmp_path), goal="g")
    result = await sc.run_sandcastle(req)

    assert result == {"status": "ok"}
    # It asked write_trusted_copy to trust the CONTAINER workspace path...
    assert seen["copy_args"][1] == sc.CONTAINER_WORKSPACE
    # ...bound that copy into the container as the .claude.json source...
    assert f"{fake_copy}:{sc.CONTAINER_CLAUDE_DIR}/.claude.json:ro" in seen["docker_args"]
    # ...and cleaned up the temp copy once the container exited.
    assert not fake_copy.exists()
