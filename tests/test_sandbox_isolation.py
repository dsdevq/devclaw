"""Sandbox config-isolation tests — the curated ~/.claude boundary.

The per-task sandbox must NOT project the whole host ~/.claude into the engineer
(that would leak personal skills/, plugins/ + their MCP servers, the global
CLAUDE.md that points at the unmounted ~/memory, and projects/ history —
non-reproducible and full of tools that fail or mislead). It mounts only a curated
allowlist, default just the OAuth credential. These pin the mount posture so the
leak can't silently come back, and assert it stays unit-testable (pure arg build,
no docker).
"""

from devclaw import sandcastle_runner as sc


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


def test_default_allowlist_is_credential_only():
    # The whole point of 1a: auth in, everything else out.
    assert sc.SANDBOX_CLAUDE_ALLOWLIST == (".credentials.json",)


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
    # ephemeral + named
    assert args[:2] == ["run", "--rm"]
    assert "--name" in args and args[args.index("--name") + 1] == "devclaw-deadbeef"
    # workspace bound
    assert f"/host/ws:{sc.CONTAINER_WORKSPACE}" in args
    # the ONLY config mount is the credential — no whole-dir leak
    assert _claude_mount_targets(args) == [
        f"{CLAUDE_DIR}/.credentials.json:{sc.CONTAINER_CLAUDE_DIR}/.credentials.json:ro"
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


# ---- API-key refusal (belt + suspenders, alongside the runner's own check) ----


def test_strip_api_keys_removes_both_vars():
    clean = sc._strip_api_keys(
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "PATH": "/bin"}
    )
    assert "ANTHROPIC_API_KEY" not in clean
    assert "ANTHROPIC_AUTH_TOKEN" not in clean
    assert clean["PATH"] == "/bin"  # unrelated env preserved
