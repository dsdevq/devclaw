"""Per-project ``sandbox_image`` override (ADR 0005, PR 2).

The escape hatch + migration bridge for the generic-sandbox tranche: a project
whose stack the mise path doesn't cover yet (or, during migration, a .NET
project waiting on the live gate) pins its own sandbox image in the registry;
every other project rides the engine's DEVCLAW_SANDBOX_IMAGE default. The
chain under test: registry field → task-queue resolution at dispatch →
EngineRequest → the docker argv both docker engines build.
"""

from __future__ import annotations

from devclaw.engine import EngineRequest
import devclaw.engine.claude_sdk as csdk
import devclaw.engine.sandcastle as sc
from devclaw.project_registry import ProjectRegistry


def _reg(tmp_path):
    return ProjectRegistry(str(tmp_path / "devclaw.db"))


# ---- registry field ----


def test_sandbox_image_persists_and_resolves(tmp_path):
    reg = _reg(tmp_path)
    reg.create(
        id="fs", name="FS", workspace_dir="/ws/fs",
        sandbox_image="devclaw-sandbox-dotnet:local",
    )
    # survives a fresh connection (real migration/read path, not a cache)
    reopened = ProjectRegistry(str(tmp_path / "devclaw.db"))
    assert reopened.get("fs").sandbox_image == "devclaw-sandbox-dotnet:local"
    assert (
        reopened.resolve_override("/ws/fs", "sandbox_image", None)
        == "devclaw-sandbox-dotnet:local"
    )


def test_unpinned_project_inherits_the_default(tmp_path):
    reg = _reg(tmp_path)
    reg.create(id="fs", name="FS", workspace_dir="/ws/fs")
    assert reg.resolve_override("/ws/fs", "sandbox_image", None) is None
    assert reg.resolve_override("/ws/unclaimed", "sandbox_image", None) is None


def test_update_can_pin_and_clear_the_override(tmp_path):
    reg = _reg(tmp_path)
    reg.create(id="fs", name="FS", workspace_dir="/ws/fs")
    reg.update("fs", sandbox_image="devclaw-sandbox-dotnet:local")
    assert reg.get("fs").sandbox_image == "devclaw-sandbox-dotnet:local"
    # omitting the kwarg leaves the pin untouched (three-way semantics)
    reg.update("fs", notes="unrelated")
    assert reg.get("fs").sandbox_image == "devclaw-sandbox-dotnet:local"
    # explicit None clears back to inherit
    reg.update("fs", sandbox_image=None)
    assert reg.get("fs").sandbox_image is None


def test_registry_rejects_flag_shaped_sandbox_image(tmp_path):
    """The argv-injection door (invariant-guard finding): a flag-shaped pin
    ("--env-file=…") would be parsed by docker as a FLAG, injecting host env —
    including a stray metered API key — into an autonomous sandbox. The
    registry write choke point rejects it loudly, for BOTH create and update,
    along with empty ("" would silently degrade to the default) and
    whitespace-ridden refs."""
    import pytest

    reg = _reg(tmp_path)
    for bad in ("--env-file=/home/user/.env", "", "img name", "-x", "a;b"):
        with pytest.raises(ValueError, match="sandbox_image"):
            reg.create(id=f"p{hash(bad) & 0xffff}", name="P", sandbox_image=bad)
    reg.create(id="ok", name="OK", workspace_dir="/ws/ok")
    for bad in ("--env-file=/x", "", " img"):
        with pytest.raises(ValueError, match="sandbox_image"):
            reg.update("ok", sandbox_image=bad)
    # legit refs (registry/tag/digest forms) still pass
    for good in (
        "devclaw-sandbox-dotnet:local",
        "ghcr.io/org/img:1.2.3",
        "img@sha256:0123456789abcdef",
    ):
        reg.update("ok", sandbox_image=good)
        assert reg.get("ok").sandbox_image == good


# ---- engine argv (both docker engines) ----


def test_sandcastle_argv_honors_the_override_and_defaults_without_it():
    base = dict(
        container_name="c",
        host_bind_path="/host/ws",
        claude_dir="/home/me/.claude",
        payload="{}",
    )
    pinned = sc._build_docker_args(**base, sandbox_image="devclaw-sandbox-dotnet:local")
    assert "devclaw-sandbox-dotnet:local" in pinned
    assert sc.SANDBOX_IMAGE not in pinned or sc.SANDBOX_IMAGE == "devclaw-sandbox-dotnet:local"
    default = sc._build_docker_args(**base)
    assert sc.SANDBOX_IMAGE in default
    # image stays in the terminal position, right before the payload
    assert pinned[-2] == "devclaw-sandbox-dotnet:local"
    assert pinned[-1] == "{}"


def test_claude_sdk_argv_honors_the_override_too():
    base = dict(
        container_name="c",
        host_bind_path="/host/ws",
        claude_dir="/home/me/.claude",
        prompt="p",
        verify_cmd=None,
    )
    pinned = csdk._build_docker_args(**base, sandbox_image="custom:img")
    assert "custom:img" in pinned
    default = csdk._build_docker_args(**base)
    assert "custom:img" not in default
    assert csdk.SANDBOX_IMAGE in default


# ---- dispatch wiring: registry pin reaches the EngineRequest ----


def test_dispatch_resolves_the_owning_projects_pin(tmp_path):
    from devclaw.task_queue import TaskQueue

    reg = _reg(tmp_path)
    reg.create(
        id="fs", name="FS", workspace_dir=str(tmp_path / "ws"),
        sandbox_image="devclaw-sandbox-dotnet:local",
    )
    q = TaskQueue.__new__(TaskQueue)  # only the resolver seam under test
    q._registry = reg
    assert q._sandbox_image(str(tmp_path / "ws")) == "devclaw-sandbox-dotnet:local"
    # unclaimed workspace → None → the engine applies its own default
    assert q._sandbox_image("/elsewhere") is None
    q._registry = None
    assert q._sandbox_image(str(tmp_path / "ws")) is None


def test_engine_request_defaults_to_no_override():
    req = EngineRequest(kind="implement_feature", workspace_dir="/ws", goal="g")
    assert req.sandbox_image is None
