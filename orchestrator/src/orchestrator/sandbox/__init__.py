"""Sandbox port — adapter selection lives here.

Callers do:

    from orchestrator.sandbox import load_sandbox
    sandbox = load_sandbox()
    result = sandbox.run(task_id=..., agent_command=[...], ...)

The selection logic reads `orchestrator/src/orchestrator/config/sandbox.yaml`. The yaml's `adapter:` field picks the live adapter; `in_process` stays as a hard-coded safety net in case sandcastle construction raises during daemon boot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from orchestrator.sandbox.base import BranchStrategy, Sandbox, SandboxResult
from orchestrator.sandbox.in_process import InProcessSandbox
from orchestrator.sandbox.sandcastle import (
    DEFAULT_IMAGE,
    DEFAULT_SANDCASTLE_VERSION,
    SandcastleConfig,
    SandcastleSandbox,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sandbox.yaml"

__all__ = [
    "Sandbox",
    "SandboxResult",
    "BranchStrategy",
    "InProcessSandbox",
    "SandcastleSandbox",
    "SandcastleConfig",
    "load_sandbox",
    "load_sandbox_config",
    "build_adapter",
    "DEFAULT_CONFIG_PATH",
]


def load_sandbox_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.warning("sandbox.yaml not found at %s; using built-in defaults", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_adapter(config: dict[str, Any]) -> Sandbox:
    """Construct the adapter named in `config['adapter']`.

    Falls back to `InProcessSandbox` on any construction error so the daemon never wedges on a bad yaml.
    """
    name = (config.get("adapter") or "in_process").strip()

    if name == "in_process":
        return InProcessSandbox()

    if name == "sandcastle":
        sc_cfg = config.get("sandcastle", {}) or {}
        try:
            return SandcastleSandbox(
                config=SandcastleConfig(
                    image=sc_cfg.get("image", DEFAULT_IMAGE),
                    version=sc_cfg.get("version", DEFAULT_SANDCASTLE_VERSION),
                    runtime=sc_cfg.get("runtime", "runsc"),
                    fallback_runtime=sc_cfg.get("fallback_runtime", "runc"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - safety net during daemon boot
            logger.warning(
                "sandcastle adapter construction failed (%s); falling back to in_process", exc
            )
            return InProcessSandbox()

    logger.warning("unknown sandbox adapter %r; falling back to in_process", name)
    return InProcessSandbox()


def load_sandbox(config_path: Path | None = None) -> Sandbox:
    """One-shot helper used by the runner. Reads yaml, builds adapter."""
    return build_adapter(load_sandbox_config(config_path))
