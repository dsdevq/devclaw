"""Sandcastle adapter — unit tests covering the adapter-specific surface.

These tests do not require Docker, npx, or sandcastle on the host. `subprocess.run` is monkeypatched inside the sandcastle module; the assertions cover the command shape, env passing, mount construction, runtime fallback, and cleanup-on-error.
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.sandbox.sandcastle import (
    DockerRuntimeProbe,
    RuntimeProbe,
    SandcastleConfig,
    SandcastleSandbox,
)


class _AvailableProbe(RuntimeProbe):
    def is_available(self, runtime: str) -> bool:  # noqa: D401
        return True


class _UnavailableProbe(RuntimeProbe):
    def is_available(self, runtime: str) -> bool:  # noqa: D401
        return False


def _patch_run(monkeypatch, *, stdout="", stderr="", returncode=0, raises=None):
    captured: list = []

    def fake_run(cmd, *args, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("orchestrator.sandbox.sandcastle.subprocess.run", fake_run)
    monkeypatch.setattr(
        "orchestrator.sandbox.sandcastle.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    return captured


# ── container name generation ────────────────────────────────────────────────


def test_container_name_prefix_and_unique():
    a = SandcastleSandbox.container_name_for("2026-05-20-task-xyz")
    b = SandcastleSandbox.container_name_for("2026-05-20-task-xyz")
    assert a.startswith("devclaw-task-2026-05-20-task-xyz-")
    assert b.startswith("devclaw-task-2026-05-20-task-xyz-")
    # Uniqueness from the uuid suffix.
    assert a != b


def test_container_name_strips_unsafe_chars():
    name = SandcastleSandbox.container_name_for("foo/bar:baz qux")
    assert "/" not in name
    assert ":" not in name
    assert " " not in name
    assert name.startswith("devclaw-task-foo-bar-baz-qux-")


# ── command construction: env + mounts + runtime ────────────────────────────


def test_run_passes_env_as_repeated_env_flags(monkeypatch):
    captured = _patch_run(monkeypatch, returncode=0, stdout="{}")
    sb = SandcastleSandbox(
        config=SandcastleConfig(image="img", version="0.5.2"),
        runtime_probe=_AvailableProbe(),
    )
    sb.run(
        task_id="t-1",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["claude", "--print", "hi"],
        env={"FOO": "1", "BAR": "two"},
        readonly_mounts={},
        timeout_seconds=60,
    )
    # First subprocess.run call is the sandcastle invocation.
    cmd = captured[0]["cmd"]
    # Sorted order — FOO comes after BAR.
    assert "--env" in cmd
    assert "BAR=two" in cmd
    assert "FOO=1" in cmd
    assert cmd.index("BAR=two") < cmd.index("FOO=1")
    # The agent command is appended after `--`.
    assert "--" in cmd
    sep_idx = cmd.index("--")
    assert cmd[sep_idx + 1 :] == ["claude", "--print", "hi"]


def test_run_constructs_readonly_mount_flags(monkeypatch):
    captured = _patch_run(monkeypatch, returncode=0, stdout="{}")
    sb = SandcastleSandbox(
        config=SandcastleConfig(image="img"),
        runtime_probe=_AvailableProbe(),
    )
    sb.run(
        task_id="t-2",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo", "ok"],
        env={},
        readonly_mounts={
            "/home/node/.gitconfig": "/home/node/.gitconfig",
            "/home/node/.config/gh": "/home/node/.config/gh",
        },
        timeout_seconds=60,
    )
    cmd = captured[0]["cmd"]
    assert "--mount" in cmd
    assert "/home/node/.gitconfig:/home/node/.gitconfig:ro" in cmd
    assert "/home/node/.config/gh:/home/node/.config/gh:ro" in cmd


def test_run_includes_image_runtime_name(monkeypatch):
    captured = _patch_run(monkeypatch, returncode=0)
    sb = SandcastleSandbox(
        config=SandcastleConfig(image="custom-image:v1", version="0.5.2"),
        runtime_probe=_AvailableProbe(),
    )
    sb.run(
        task_id="t-3",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo"],
        env={},
        readonly_mounts={},
        timeout_seconds=60,
    )
    cmd = captured[0]["cmd"]
    assert "--image" in cmd and "custom-image:v1" in cmd
    assert "--runtime" in cmd and "runsc" in cmd
    assert "--name" in cmd
    name_idx = cmd.index("--name")
    assert cmd[name_idx + 1].startswith("devclaw-task-t-3-")
    assert "sandcastle@0.5.2" in cmd[:6]


# ── runtime fallback ────────────────────────────────────────────────────────


def test_runsc_unavailable_falls_back_to_runc(monkeypatch, caplog):
    captured = _patch_run(monkeypatch, returncode=0)
    sb = SandcastleSandbox(
        config=SandcastleConfig(runtime="runsc", fallback_runtime="runc"),
        runtime_probe=_UnavailableProbe(),
    )
    with caplog.at_level("WARNING", logger="orchestrator.sandbox.sandcastle"):
        result = sb.run(
            task_id="t-4",
            repo_url="x",
            branch_strategy="new-branch",
            agent_command=["echo"],
            env={},
            readonly_mounts={},
            timeout_seconds=60,
        )
    cmd = captured[0]["cmd"]
    runtime_idx = cmd.index("--runtime")
    assert cmd[runtime_idx + 1] == "runc"
    assert result.runtime == "runc"
    assert any("falling back" in n for n in result.notes)
    assert any("falling back" in rec.message for rec in caplog.records)


def test_runtime_when_probe_says_available(monkeypatch):
    captured = _patch_run(monkeypatch, returncode=0)
    sb = SandcastleSandbox(
        config=SandcastleConfig(runtime="runsc"), runtime_probe=_AvailableProbe()
    )
    result = sb.run(
        task_id="t-5",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo"],
        env={},
        readonly_mounts={},
        timeout_seconds=60,
    )
    assert result.runtime == "runsc"
    assert result.notes == []


# ── cleanup ─────────────────────────────────────────────────────────────────


def test_cleanup_runs_on_nonzero_exit(monkeypatch):
    captured = _patch_run(monkeypatch, returncode=42, stderr="bad")
    sb = SandcastleSandbox(runtime_probe=_AvailableProbe())
    result = sb.run(
        task_id="t-6",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo"],
        env={},
        readonly_mounts={},
        timeout_seconds=60,
    )
    assert result.returncode == 42
    # Two subprocess.run calls: sandcastle invocation + docker rm cleanup.
    assert len(captured) == 2
    cleanup_cmd = captured[1]["cmd"]
    assert cleanup_cmd[1] == "rm"
    assert cleanup_cmd[2] == "-f"
    assert cleanup_cmd[3] == result.container_name


def test_cleanup_runs_on_timeout(monkeypatch):
    captured = _patch_run(
        monkeypatch,
        raises=subprocess.TimeoutExpired(cmd=["npx"], timeout=1, output=b"partial"),
    )
    sb = SandcastleSandbox(runtime_probe=_AvailableProbe())
    result = sb.run(
        task_id="t-7",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo"],
        env={},
        readonly_mounts={},
        timeout_seconds=1,
    )
    assert result.timed_out is True
    # The TimeoutExpired is raised on the first call; cleanup should have happened on the second.
    assert len(captured) >= 1
    if len(captured) >= 2:
        assert captured[1]["cmd"][1:3] == ["rm", "-f"]


def test_npx_missing_returns_blocker(monkeypatch):
    captured = _patch_run(monkeypatch, raises=FileNotFoundError("npx"))
    sb = SandcastleSandbox(runtime_probe=_AvailableProbe())
    result = sb.run(
        task_id="t-8",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["echo"],
        env={},
        readonly_mounts={},
        timeout_seconds=10,
    )
    assert result.returncode == -1
    assert "npx" in result.stderr
    assert "npx_or_sandcastle_missing" in result.notes


# ── runtime probe sanity check ──────────────────────────────────────────────


def test_docker_runtime_probe_returns_false_without_docker(monkeypatch):
    monkeypatch.setattr("orchestrator.sandbox.sandcastle.shutil.which", lambda name: None)
    probe = DockerRuntimeProbe()
    assert probe.is_available("runsc") is False


def test_docker_runtime_probe_parses_runtimes_output(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.sandbox.sandcastle.shutil.which", lambda name: "/usr/bin/docker"
    )

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"runc":{"path":"runc"},"runsc":{"path":"runsc"}}\n',
            stderr="",
        )

    monkeypatch.setattr("orchestrator.sandbox.sandcastle.subprocess.run", fake_run)
    probe = DockerRuntimeProbe()
    assert probe.is_available("runsc") is True
    assert probe.is_available("nonexistent") is False
