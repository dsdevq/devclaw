"""durable deploy hosting — naming, deterministic ports, durable run args, the
tailscale serve seam, and the start happy-path.

``_run`` is monkeypatched so no docker/tailscale is touched; ``asyncio.sleep`` is
neutralized so the readiness loop doesn't actually wait."""
from __future__ import annotations

import pytest

from devclaw.delivery import deploy


def test_deploy_name_slugifies():
    assert deploy.deploy_name("closeloop") == "devclaw-deploy-closeloop"
    assert deploy.deploy_name("My App!") == "devclaw-deploy-My-App"
    assert deploy.deploy_name("///") == "devclaw-deploy-app"


def test_deploy_port_is_deterministic_and_in_range():
    # Same slug → same port across calls (stable handoff URL across redeploys).
    assert deploy.deploy_port("closeloop") == deploy.deploy_port("closeloop")
    p = deploy.deploy_port("closeloop")
    assert deploy.DEPLOY_PORT_BASE <= p < deploy.DEPLOY_PORT_BASE + deploy.DEPLOY_PORT_SPAN
    # Different slugs generally land on different ports.
    assert deploy.deploy_port("closeloop") != deploy.deploy_port("todo-fullstack")


def test_build_run_args_is_durable_and_loopback_only():
    port = deploy.deploy_port("x")
    args = deploy._build_run_args(name="devclaw-deploy-x", host_path="/srv/ws/x", port=port)
    j = " ".join(args)
    assert "-d" in args and "--name" in args
    assert "--memory" in args and deploy.DEPLOY_MEMORY in args  # hard cap
    assert "--cpus" in args
    # The durability delta vs a preview:
    assert "unless-stopped" in args and "--restart" in args
    assert "devclaw.deploy=1" in args  # listed/reaped separately from previews
    assert f"127.0.0.1:{port}:8000" in j  # loopback-only publish
    assert "/srv/ws/x:/app" in j
    assert args[-1] == deploy._LAUNCHER and "--entrypoint" in args


def test_serve_command_is_idempotent_one_liner():
    cmd = deploy.serve_command(8217)
    assert "tailscale serve" in cmd and "--https=8217" in cmd and "127.0.0.1:8217" in cmd


def test_ready_probes_root_not_docs():
    # Regression: closeloop serves `/` 200 but has no `/docs` (Swagger). The
    # readiness probe must default to `/`, else healthy apps report not-ready.
    import inspect
    assert inspect.signature(deploy._ready).parameters["path"].default == "/"


@pytest.mark.asyncio
async def test_tailnet_dns_name_tolerates_version_skew_warning(monkeypatch):
    # Regression: `tailscale` prints a non-fatal version-skew warning to stderr,
    # which `_run` folds into stdout — prepending non-JSON. The parse must slice
    # from the first `{` (this is why the first live deploy missed the DNS name).
    polluted = (
        'Warning: client version "1.98.4" != tailscaled server version "1.98.3"\n'
        '{"Self": {"DNSName": "lifekit-vps.tail1cb676.ts.net."}}'
    )

    async def fake_run(bin_, *args):
        return 0, polluted

    monkeypatch.setattr(deploy, "_run", fake_run)
    assert await deploy._tailnet_dns_name() == "lifekit-vps.tail1cb676.ts.net"


@pytest.mark.asyncio
async def test_tailnet_dns_name_none_when_no_json(monkeypatch):
    async def fake_run(bin_, *args):
        return 0, "tailscale: not logged in"   # no JSON object at all

    monkeypatch.setattr(deploy, "_run", fake_run)
    assert await deploy._tailnet_dns_name() is None


@pytest.fixture()
def _no_sleep(monkeypatch):
    async def _fast(_):
        return None
    monkeypatch.setattr(deploy.asyncio, "sleep", _fast)


@pytest.mark.asyncio
async def test_deploy_project_happy_path_with_tailscale(monkeypatch, _no_sleep):
    docker_calls, ts_calls = [], []

    async def fake_run(bin_, *args):
        if bin_ == deploy.TAILSCALE_BIN:
            ts_calls.append(args)
            if args[0] == "serve":
                return 0, ""
            if args[0] == "status":
                return 0, '{"Self": {"DNSName": "lifekit-vps.tailnet-abc.ts.net."}}'
            return 0, ""
        docker_calls.append(args)
        if args[0] == "ps":
            return 0, ""             # no existing deploys → no eviction
        if args[0] == "run":
            return 0, "deadbeef"
        if args[0] == "inspect":
            return 0, "true"         # container running
        if args[0] == "exec":
            return 0, ""             # ready
        return 0, ""                 # rm, etc.

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_translate_workspace_path", lambda p: "/srv/ws/closeloop")

    out = await deploy.deploy_project("/var/lib/devclaw/workspaces/closeloop", "closeloop")

    assert out["container"] == "devclaw-deploy-closeloop"
    assert out["ready"] is True
    assert out["tailscale_served"] is True
    # Stable Tailscale https URL at the deterministic per-slug port, trailing dot stripped.
    assert out["url"] == f"https://lifekit-vps.tailnet-abc.ts.net:{out['port']}/"
    assert out["api_docs_url"].endswith(f":{out['port']}/docs")
    assert any(c[0] == "run" for c in docker_calls)
    assert any(c[0] == "serve" for c in ts_calls)


@pytest.mark.asyncio
async def test_deploy_project_falls_back_when_tailscale_absent(monkeypatch, _no_sleep):
    async def fake_run(bin_, *args):
        if bin_ == deploy.TAILSCALE_BIN:
            return 127, "tailscale not runnable"   # CLI/socket unreachable from here
        if args[0] == "ps":
            return 0, ""
        if args[0] == "run":
            return 0, "id"
        if args[0] == "inspect":
            return 0, "true"
        if args[0] == "exec":
            return 0, ""
        return 0, ""

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_translate_workspace_path", lambda p: "/srv/ws/closeloop")

    out = await deploy.deploy_project("/var/lib/devclaw/workspaces/closeloop", "closeloop")

    assert out["tailscale_served"] is False
    assert "url" not in out                      # no public URL minted
    assert out["loopback_url"].endswith(f":{out['port']}/")
    assert out["serve_command"] in out["note"]   # the one-time manual command is surfaced


@pytest.mark.asyncio
async def test_deploy_project_evicts_oldest_over_cap(monkeypatch, _no_sleep):
    monkeypatch.setattr(deploy, "DEPLOY_MAX", 2)
    removed = []

    async def fake_run(bin_, *args):
        if bin_ == deploy.TAILSCALE_BIN:
            if args and args[0] == "status":
                return 0, '{"Self": {"DNSName": "n.t.ts.net."}}'
            return 0, ""
        if args[0] == "ps":
            return 0, "devclaw-deploy-c\ndevclaw-deploy-b\ndevclaw-deploy-a"
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

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_translate_workspace_path", lambda p: "/srv/ws/x")

    out = await deploy.deploy_project("/var/lib/devclaw/workspaces/new", "new")
    # cap=2, one new starting → at least one of the 3 existing must be evicted (oldest first).
    assert "devclaw-deploy-a" in out["evicted"]
