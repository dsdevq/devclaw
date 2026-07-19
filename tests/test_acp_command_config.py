"""The ACP agent command is a configurable seam, not a hardcode.

The worker layer's model-agnosticism claim used to rest on a literal
``acp_command=["claude-agent-acp"]`` inside the runner — swapping the agent
meant editing code. Now the command is resolved payload → env → default, and
the host threads it through the runner JSON payload exactly like ``model``
(host env vars do NOT cross the container boundary, so a payload ride is the
only channel that works in the sandbox). These pin that seam.

The runner lives at openhands-runner/runner.py (not a package); its
openhands-sdk imports are inside main(), so a top-level import is SDK-free.
"""

import importlib.util
from pathlib import Path

import pytest

from devclaw.engine import EngineRequest
from devclaw.engine import sandcastle

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_acp_test", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # executes top-level only; main() is not __main__
    return mod


# ---- runner-side resolution: payload → env → default ----


def test_acp_command_defaults_to_claude_agent_acp(runner, monkeypatch):
    monkeypatch.delenv("DEVCLAW_ACP_COMMAND", raising=False)
    assert runner._resolve_acp_command({}) == ["claude-agent-acp"]
    # a null payload key (host default posture) is the same as absent
    assert runner._resolve_acp_command({"acp_command": None}) == ["claude-agent-acp"]


def test_acp_command_payload_overrides_env(runner, monkeypatch):
    monkeypatch.setenv("DEVCLAW_ACP_COMMAND", "env-agent")
    assert runner._resolve_acp_command({"acp_command": "payload-agent"}) == [
        "payload-agent"
    ]


def test_acp_command_env_fallback_for_manual_runs(runner, monkeypatch):
    # a manual `docker run` / host-engine run has no payload key; env applies
    monkeypatch.setenv("DEVCLAW_ACP_COMMAND", "env-agent --flag")
    assert runner._resolve_acp_command({}) == ["env-agent", "--flag"]


def test_acp_command_string_is_shlex_split(runner, monkeypatch):
    monkeypatch.delenv("DEVCLAW_ACP_COMMAND", raising=False)
    assert runner._resolve_acp_command(
        {"acp_command": "my-acp --profile 'a b'"}
    ) == ["my-acp", "--profile", "a b"]


def test_acp_command_unbalanced_quote_raises_for_loud_failure(runner, monkeypatch):
    # shlex refuses a malformed spec; main() turns this into a structured
    # `result: {"status": "error", ...}` naming the knob — never a silent
    # fallback to the default agent (a typo must not quietly swap agents).
    monkeypatch.delenv("DEVCLAW_ACP_COMMAND", raising=False)
    with pytest.raises(ValueError):
        runner._resolve_acp_command({"acp_command": "bad 'quote"})


def test_acp_command_blank_string_falls_back_to_default(runner, monkeypatch):
    # an operator exporting DEVCLAW_ACP_COMMAND="" must not produce argv []
    monkeypatch.setenv("DEVCLAW_ACP_COMMAND", "   ")
    assert runner._resolve_acp_command({"acp_command": ""}) == ["claude-agent-acp"]


# ---- host-side payload: the seam crosses the container boundary as data ----


def _req() -> EngineRequest:
    return EngineRequest(
        kind="implement_feature", workspace_dir="/tmp/ws", goal="do the thing"
    )


def test_sandcastle_payload_carries_acp_command(monkeypatch):
    monkeypatch.setattr(sandcastle, "ACP_COMMAND", "other-acp --fast")
    assert sandcastle._build_payload(_req())["acp_command"] == "other-acp --fast"


def test_sandcastle_payload_acp_command_defaults_to_none(monkeypatch):
    # unset env → None in the payload → the runner's claude default applies;
    # the shipped default posture must never pin a different agent silently
    monkeypatch.setattr(sandcastle, "ACP_COMMAND", None)
    payload = sandcastle._build_payload(_req())
    assert payload["acp_command"] is None
    # the rest of the contract is untouched by the new key
    assert payload["kind"] == "implement_feature"
    assert payload["workspace_dir"] == sandcastle.CONTAINER_WORKSPACE
    assert payload["goal"] == "do the thing"


def test_shipped_default_is_unset():
    # ACP_COMMAND mirrors EXEC_MODEL's "env at import" pattern; the shipped
    # default must be unset (→ claude-agent-acp downstream). Same test-env
    # assumption as the EXEC_MODEL assertion in test_model_tiering.py.
    assert sandcastle.ACP_COMMAND is None
