"""Claude-SDK engine tests — pure mechanism (no docker, no claude).

Covers the docker argv assembly + the stdout-stream parser. The live eval
comparison vs OpenHands lives in ``evals/`` (real engine + real claude); these
tests guard the mechanism the live driver depends on.
"""

from __future__ import annotations

from devclaw.claude_sdk_engine import _build_docker_args, _prompt, _stream_output
from devclaw.engine import EngineRequest


def test_prompt_picks_template_per_kind():
    req = EngineRequest(kind="fix_bug", workspace_dir="/ws", goal="Off-by-one in pager")
    p = _prompt(req)
    assert "READ existing code" in p
    assert "Off-by-one in pager" in p
    assert "BLOCKED" in p


def test_prompt_falls_back_for_unknown_kind():
    req = EngineRequest(kind="implement_feature", workspace_dir="/ws", goal="X")
    p = _prompt(req)
    assert "Implement the following" in p
    assert "X" in p


def test_docker_args_have_curated_auth_mounts_and_no_api_keys():
    args = _build_docker_args(
        container_name="devclaw-test",
        host_bind_path="/host/ws",
        claude_dir="/home/me/.claude",
        prompt="hello",
        verify_cmd=None,
    )
    joined = " ".join(args)
    assert "--rm" in args
    assert "/host/ws:/workspace" in joined
    # the curated allowlist default (credential + identity) makes it in RO
    assert "/home/me/.claude/.credentials.json:/home/agent/.claude/.credentials.json:ro" in joined
    assert "/home/me/.claude/.claude.json:/home/agent/.claude/.claude.json:ro" in joined
    # never project an API key — Pro OAuth posture
    assert "ANTHROPIC_API_KEY" not in joined


def test_docker_args_inline_verify_cmd_inside_container():
    args = _build_docker_args(
        container_name="devclaw-test",
        host_bind_path="/host/ws",
        claude_dir="/home/me/.claude",
        prompt="do thing",
        verify_cmd="cd backend && dotnet test",
    )
    inner = args[-1]
    assert "__VERIFY_BEGIN__" in inner
    assert "cd backend && dotnet test" in inner
    assert "result:" in inner  # the JSON result line is emitted by the inner shell


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = self  # the stream attr below points at us
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
        return gen()


async def test_stream_output_parses_agent_lines_then_result_with_verify():
    proc = _FakeProc([
        b"reading source files\n",
        b"applied patch to backend/Foo.cs\n",
        b"DONE\n",
        b"__VERIFY_BEGIN__\n",
        b"Test Run Successful.\n",
        b"Passed: 12, Failed: 0\n",
        b'result: {"status":"ok","workspaceDir":"/workspace","agent_exit":0,"verify":{"ran":true,"cmd":"dotnet test","exit_code":0,"passed":true}}\n',
    ])
    events: list = []
    result, _agent_out = await _stream_output(proc, events.append)
    assert result["status"] == "ok"
    assert result["verify"]["passed"] is True
    assert result["verify"]["output"].startswith("Test Run Successful")
    assert "applied patch" in result["agent_output"]
    # StdoutLine events fired only for the AGENT phase, not verify
    assert all(e.type == "StdoutLine" for e in events)
    assert len(events) == 3


async def test_stream_output_synthesizes_error_when_no_result_line():
    proc = _FakeProc([b"some output\n", b"no result line\n"])
    result, _ = await _stream_output(proc, None)
    assert result["status"] == "error"
    assert "no result line" in result["error"]
    assert "some output" in result["agent_output"]
