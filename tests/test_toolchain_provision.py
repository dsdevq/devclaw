"""Project-declared toolchain provisioning (ADR 0005).

The sandbox image ships no language SDKs beyond python+node; the runner's
pre-step detects the project's declared toolchain (mise-native files, or
translated global.json / package.json engines), `mise install`s it, and
exports the resulting environment so the agent's shells and the verify gate
inherit it. These tests pin the contract:

  - detection reads ONLY the workspace's declaration files;
  - no declaration → zero-cost no-op (no subprocess at all);
  - a declared toolchain with no mise on PATH fails CLOSED (the deploy-skew
    class — lifekit-stack#93 — must never silently degrade to python+node);
  - translated declarations are recorded OUTSIDE the workspace (a generated
    file in /workspace would dirty the diff the review gate sees);
  - the per-project cache volume mount is part of the docker argv posture.

The runner lives at openhands-runner/runner.py (not a package); its
openhands-sdk imports are inside main(), so a top-level import is SDK-free.
"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

import devclaw.engine.sandcastle as sc

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_toolchain", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def restore_env():
    """_provision_toolchain deliberately mutates os.environ (that's the export
    contract); undo it so tests stay independent."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


# ---- detection ----


def test_no_declaration_detects_nothing(runner, tmp_path):
    assert runner._detect_toolchain(str(tmp_path)) == (False, {})


@pytest.mark.parametrize("name", [".mise.toml", "mise.toml", ".tool-versions"])
def test_mise_native_file_wins_untranslated(runner, tmp_path, name):
    (tmp_path / name).write_text("dotnet = '9.0'\n")
    # a global.json alongside is NOT translated — mise reads its own file as-is
    (tmp_path / "global.json").write_text('{"sdk": {"version": "8.0.100"}}')
    assert runner._detect_toolchain(str(tmp_path)) == (True, {})


def test_global_json_translates_to_fuzzy_major_minor(runner, tmp_path):
    # fuzzy on purpose: rollForward reality means any 9.0.x satisfies 9.0.203
    (tmp_path / "global.json").write_text('{"sdk": {"version": "9.0.203"}}')
    assert runner._detect_toolchain(str(tmp_path)) == (False, {"dotnet": "9.0"})


def test_global_json_without_sdk_version_declares_nothing(runner, tmp_path):
    (tmp_path / "global.json").write_text('{"msbuild-sdks": {}}')
    assert runner._detect_toolchain(str(tmp_path)) == (False, {})


def test_corrupt_global_json_fails_closed_not_skipped(runner, tmp_path):
    (tmp_path / "global.json").write_text('{"sdk": {')
    with pytest.raises(runner.ToolchainError, match="global.json"):
        runner._detect_toolchain(str(tmp_path))


def test_package_json_engines_node_translates_minimum(runner, tmp_path):
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=20.11"}}')
    assert runner._detect_toolchain(str(tmp_path)) == (False, {"node": "20.11"})


def test_package_json_without_engines_declares_nothing(runner, tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    assert runner._detect_toolchain(str(tmp_path)) == (False, {})


def test_translations_combine_across_files(runner, tmp_path):
    (tmp_path / "global.json").write_text('{"sdk": {"version": "10.0.100"}}')
    (tmp_path / "package.json").write_text('{"engines": {"node": "^22.1"}}')
    assert runner._detect_toolchain(str(tmp_path)) == (
        False,
        {"dotnet": "10.0", "node": "22.1"},
    )


# ---- provisioning ----


def test_undeclared_workspace_is_a_zero_cost_noop(runner, tmp_path, monkeypatch):
    """No declaration → None, and NO subprocess is ever spawned (the tick-path
    analogue of the zero-token idle guard: idle projects pay nothing)."""

    def _boom(*a, **k):  # pragma: no cover - the assertion IS that it's unreached
        raise AssertionError("provisioning ran a subprocess on an undeclared workspace")

    monkeypatch.setattr(runner.subprocess, "run", _boom)
    monkeypatch.setattr(runner.shutil, "which", _boom)
    assert runner._provision_toolchain(str(tmp_path)) is None


def test_declared_toolchain_without_mise_fails_closed(runner, tmp_path, monkeypatch):
    """The deploy-skew class (lifekit-stack#93): new runner + stale image with
    no mise must be a loud error, never a silent python+node fallback."""
    (tmp_path / ".tool-versions").write_text("nodejs 22.1.0\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(runner.ToolchainError, match="mise.*not on PATH"):
        runner._provision_toolchain(str(tmp_path))


def _fake_mise(calls, *, install_rc=0, env_json='{"PATH": "/fake/shims"}'):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["mise", "install"]:
            return subprocess.CompletedProcess(argv, install_rc, stdout="", stderr="boom")
        if argv[:2] == ["mise", "env"]:
            return subprocess.CompletedProcess(argv, 0, stdout=env_json, stderr="")
        raise AssertionError(f"unexpected subprocess: {argv}")

    return run


def test_native_declaration_installs_in_workspace_trusted(
    runner, tmp_path, monkeypatch, restore_env
):
    (tmp_path / ".tool-versions").write_text("nodejs 22.1.0\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/mise")
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", _fake_mise(calls))

    summary = runner._provision_toolchain(str(tmp_path))

    assert summary is not None and summary["native"] is True and summary["tools"] == {}
    install_argv, install_kwargs = calls[0]
    assert install_argv == ["mise", "install"]
    # cwd = the workspace (mise reads the project's own file), and the
    # workspace is trusted so non-interactive mise doesn't refuse its config
    assert install_kwargs["cwd"] == str(tmp_path)
    assert install_kwargs["env"]["MISE_TRUSTED_CONFIG_PATHS"] == str(tmp_path)
    # native path records NO translated config
    assert "MISE_GLOBAL_CONFIG_FILE" not in os.environ


def test_translated_declaration_never_touches_the_workspace(
    runner, tmp_path, monkeypatch, restore_env
):
    """global.json → mise config OUTSIDE /workspace: a generated file in the
    workspace would dirty the diff the review gate and delivery see."""
    (tmp_path / "global.json").write_text('{"sdk": {"version": "9.0.203"}}')
    before = set(os.listdir(tmp_path))
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/mise")
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", _fake_mise(calls))

    summary = runner._provision_toolchain(str(tmp_path))

    assert summary is not None and summary["tools"] == {"dotnet": "9.0"}
    assert set(os.listdir(tmp_path)) == before  # workspace byte-untouched
    cfg = os.environ.get("MISE_GLOBAL_CONFIG_FILE")
    assert cfg and not cfg.startswith(str(tmp_path))
    assert 'dotnet = "9.0"' in Path(cfg).read_text()


def test_provisioned_env_is_exported_for_agent_and_verify_gate(
    runner, tmp_path, monkeypatch, restore_env
):
    (tmp_path / ".tool-versions").write_text("dotnet 9.0\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/mise")
    calls = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_mise(calls, env_json=json.dumps({"PATH": "/shims:/usr/bin", "DOTNET_ROOT": "/tools/dotnet"})),
    )

    runner._provision_toolchain(str(tmp_path))

    # the whole point: the agent's shells AND verify_cmd inherit the toolchain
    assert os.environ["PATH"] == "/shims:/usr/bin"
    assert os.environ["DOTNET_ROOT"] == "/tools/dotnet"


def test_mise_env_export_strips_api_keys(runner, tmp_path, monkeypatch, restore_env):
    """OAuth-only invariant: `mise env` reflects the (trusted) workspace's own
    [env] config, so a project's .mise.toml could reintroduce a metered API
    key AFTER _refuse_api_key() already passed. The export loop must re-apply
    the denylist — a stray key must never silently switch autonomous runs
    onto metered billing."""
    (tmp_path / ".tool-versions").write_text("nodejs 22\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/mise")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_mise(
            [],
            env_json=json.dumps(
                {
                    "PATH": "/shims",
                    "ANTHROPIC_API_KEY": "sk-ant-leak",
                    "ANTHROPIC_AUTH_TOKEN": "leak-token",
                }
            ),
        ),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    runner._provision_toolchain(str(tmp_path))

    assert os.environ["PATH"] == "/shims"  # legit vars still exported
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_mise_install_failure_is_legible_and_closed(
    runner, tmp_path, monkeypatch, restore_env
):
    (tmp_path / ".tool-versions").write_text("nodejs 22\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/mise")
    monkeypatch.setattr(runner.subprocess, "run", _fake_mise([], install_rc=1))
    with pytest.raises(runner.ToolchainError, match=r"mise install.*exit 1.*boom"):
        runner._provision_toolchain(str(tmp_path))


# ---- the per-project cache volume (sandcastle side) ----


def test_toolchain_volume_name_is_deterministic_and_project_scoped():
    a = sc._toolchain_volume_name("/srv/devclaw/workspaces/finance-sentry")
    assert a == sc._toolchain_volume_name("/srv/devclaw/workspaces/finance-sentry")
    assert a.startswith("devclaw-toolchains-finance-sentry-")
    # a DIFFERENT project (even same basename elsewhere) gets its OWN volume —
    # per-project isolation was the lock decision, over a shared cache
    b = sc._toolchain_volume_name("/elsewhere/finance-sentry")
    assert b != a
    # docker volume name grammar
    import re

    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", a)


def test_toolchain_volume_name_sanitizes_hostile_basenames():
    v = sc._toolchain_volume_name("/srv/ws/My Repo (v2)!")
    assert v.startswith("devclaw-toolchains-my-repo-v2-")
    import re

    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", v)


def test_docker_args_mount_the_project_toolchain_cache():
    args = sc._build_docker_args(
        container_name="c",
        host_bind_path="/host/ws",
        claude_dir="/home/me/.claude",
        payload="{}",
    )
    expected = f"{sc._toolchain_volume_name('/host/ws')}:{sc.CONTAINER_MISE_DATA}"
    assert expected in args
    assert args[args.index(expected) - 1] == "-v"
