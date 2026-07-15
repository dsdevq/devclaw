"""The suite-wide no-real-docker guard (tests/conftest.py::_block_real_docker).

On 2026-07-14 pytest runs on two hosts each leaked a REAL running container
``devclaw-deploy-g`` (image devclaw-sandbox:latest, ``--restart unless-stopped``,
publishing a host port): a done-gate tick test reached the real
``deploy.deploy_project`` because ``_auto_deploy`` defaults ``enabled=True`` and
swallows ``Exception`` — so on a docker-enabled host the deploy silently
SUCCEEDED and outlived the test process. These tests pin the structural fix:
any real docker/tailscale spawn fails the test loudly, while the pure helpers
and fake-runner injection seams stay usable.
"""

import asyncio

import pytest

from devclaw.delivery import deploy
from devclaw.engine import sandcastle as sc


def test_suite_guard_blocks_real_docker_invocations():
    """The chokepoint every deploy docker call goes through (`deploy._docker` →
    `deploy._run` → asyncio.create_subprocess_exec) must fail the test with a
    BaseException — an `except Exception` best-effort path (e.g.
    tick_donegate._auto_deploy) must NOT be able to swallow it."""
    with pytest.raises(pytest.fail.Exception, match="BLOCKED.*docker"):
        asyncio.run(deploy._docker("ps", "-a"))


def test_suite_guard_blocks_real_tailscale_invocations():
    with pytest.raises(pytest.fail.Exception, match="BLOCKED.*tailscale"):
        asyncio.run(deploy._run(deploy.TAILSCALE_BIN, "status", "--json"))


def test_suite_guard_blocks_sandcastle_sync_docker_seam():
    """The sweep's synchronous seam is a separate escape hatch (subprocess.run,
    not asyncio) — guarded independently."""
    with pytest.raises(pytest.fail.Exception, match="BLOCKED.*_docker_run_sync"):
        sc._docker_run_sync(["ps", "--filter", "label=devclaw.sandbox=1"])


def test_suite_guard_lets_non_docker_subprocesses_through():
    """git/gh/python spawns are legitimate (many tests `git init` real repos) —
    the guard blocks by program basename, not wholesale."""
    import sys

    async def _echo() -> int:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait()

    assert asyncio.run(_echo()) == 0


def test_pure_deploy_helpers_stay_testable():
    """deploy_name / deploy_port / _build_run_args are pure — no subprocess —
    and must keep working under the guard."""
    assert deploy.deploy_name("g") == "devclaw-deploy-g"
    port = deploy.deploy_port("g")
    assert deploy.DEPLOY_PORT_BASE <= port < deploy.DEPLOY_PORT_BASE + deploy.DEPLOY_PORT_SPAN
    assert deploy.deploy_port("g") == port  # deterministic across calls
    args = deploy._build_run_args(name="devclaw-deploy-g", host_path="/srv/ws/g", port=port)
    assert args[:2] == ["run", "-d"]
    assert "devclaw.deploy=1" in args


def test_fake_runner_injection_still_bypasses_guard(monkeypatch):
    """Tests that inject a fake `_run` (the whole of test_deploy.py) replace the
    guarded seam and must keep passing without ever reaching the guard."""
    calls = []

    async def fake_run(bin_, *args):
        calls.append((bin_, args))
        return 0, "true"

    monkeypatch.setattr(deploy, "_run", fake_run)
    rc, out = asyncio.run(deploy._docker("inspect", "-f", "{{.State.Running}}", "x"))
    assert (rc, out) == (0, "true")
    assert calls and calls[0][0] == deploy.DOCKER_BIN
