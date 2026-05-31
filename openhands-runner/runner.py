"""
DevClaw — OpenHands runner (runs inside the per-task sandbox container).

Spawned by the host sandcastle runner via ``docker run``. Reads a single JSON
request from argv[1] and streams progress to stdout, one prefixed line at a
time:

    event: {"id":"...","type":"ActionEvent","source":"agent","payload":{...},"ts":...}
    event: {"id":"...","type":"ObservationEvent",...}
    ...
    result: {"status":"ok","workspace_dir":"...","message":"..."}

The TS caller splits on newlines and routes `event:` lines to the events
table while waiting for the single terminating `result:` line. On failure
the `result:` line carries status='error' instead.

Authentication: Claude Code OAuth session via CLAUDE_CODE_EXECUTABLE +
CLAUDE_CONFIG_DIR env vars. No ANTHROPIC_API_KEY required or accepted.
"""

import contextlib
import io
import json
import os
import sys
import time
import traceback


_KIND_WRAPPERS = {
    "implement_feature": (
        # No wrapper for implement_feature — the user's goal IS the instruction.
        "{goal}"
    ),
    "fix_bug": (
        "You are fixing a bug. Read the existing code in the current workspace "
        "first to understand what's there before making changes. Make the "
        "smallest change that fixes the bug; do NOT refactor unrelated code. "
        "After fixing, run whatever test suite exists in the project to confirm "
        "your fix works.\n\n"
        "Bug description:\n{goal}"
    ),
    "review_repository": (
        "You are reviewing this repository — READ ONLY. Do NOT modify, create, "
        "or delete any files in the workspace. Your only allowed actions are "
        "reading files and running read-only inspection commands "
        "(ls, cat, grep, git log, git diff, etc.). At the end, write a clear "
        "review report to STDOUT in your final message covering: codebase "
        "summary, concerns or bugs you noticed, suggested improvements. If a "
        "specific focus area was provided, address that first.\n\n"
        "Review focus (if any):\n{goal}"
    ),
}


def _wrap_goal(kind: str, goal: str) -> str:
    template = _KIND_WRAPPERS.get(kind, _KIND_WRAPPERS["implement_feature"])
    return template.format(goal=goal)


# `sys.__stdout__` is the original stdout the process was started with —
# `contextlib.redirect_stdout` swaps `sys.stdout` but leaves `__stdout__`
# alone. We write our prefixed protocol lines (`event:` / `result:`)
# straight to it so SDK decorative output captured by the redirect block
# can't swallow them.
_PROTO_OUT = sys.__stdout__


def _emit_result(payload: dict) -> None:
    """Write the final terminating `result: <json>` line and flush.

    The TS caller treats the first `result:` line as the run's verdict.
    Anything written to stdout AFTER this line is ignored.
    """
    _PROTO_OUT.write("result: " + json.dumps(payload) + "\n")
    _PROTO_OUT.flush()


def _emit_event(payload: dict) -> None:
    """Write one `event: <json>` line and flush.

    Flushing matters: the TS caller streams stdout line-by-line and writes
    each event to the events table the moment it arrives. Without flush
    we'd see a flood of events only at process exit.
    """
    _PROTO_OUT.write("event: " + json.dumps(payload) + "\n")
    _PROTO_OUT.flush()


def _refuse_api_key() -> None:
    """Refuse to run if an API key snuck into the env — preserves the
    Pro-subscription cost model (memory: pro-subscription-is-the-design)."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            _emit_result(
                {
                    "status": "error",
                    "error": (
                        f"{var} is set in the environment. DevClaw v2 runs "
                        "exclusively through Claude Code OAuth — refusing to "
                        "spend metered credits."
                    ),
                }
            )
            sys.exit(2)


def main() -> None:
    _refuse_api_key()

    if len(sys.argv) != 2:
        _emit_result({"status": "error", "error": "expected one JSON arg"})
        sys.exit(2)

    try:
        req = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        _emit_result({"status": "error", "error": f"invalid JSON: {exc}"})
        sys.exit(2)

    workspace_dir = req.get("workspace_dir")
    goal = req.get("goal")
    kind = req.get("kind", "implement_feature")
    # Model tier for the agent. The host passes it in the payload; fall back to
    # DEVCLAW_EXEC_MODEL for a manual `docker run`. None → the ACP server default.
    acp_model = req.get("model") or os.environ.get("DEVCLAW_EXEC_MODEL") or None
    if not workspace_dir or not goal:
        _emit_result(
            {
                "status": "error",
                "error": "request must include workspace_dir and goal",
            }
        )
        sys.exit(2)

    if kind not in ("implement_feature", "fix_bug", "review_repository"):
        _emit_result({"status": "error", "error": f"unknown kind: {kind}"})
        sys.exit(2)

    # Wrap the user's goal with kind-specific operating instructions. The
    # OpenHands ACP-driven Claude session reads this as the user message,
    # so prepending instructions here is the cheapest way to bias behavior
    # without a custom system prompt.
    wrapped_goal = _wrap_goal(kind, goal)

    os.makedirs(workspace_dir, exist_ok=True)

    # Default to a PATH lookup — inside the sandbox the Dockerfile sets
    # CLAUDE_CODE_EXECUTABLE=/usr/bin/claude, so this fallback only matters for
    # host/misconfigured runs. (Was a hardcoded personal path — a leak + footgun.)
    claude_exec = os.environ.get("CLAUDE_CODE_EXECUTABLE") or "claude"
    claude_cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")

    try:
        from openhands.sdk.agent import ACPAgent
        from openhands.sdk.conversation import Conversation
        from openhands.sdk.event.base import Event
    except ImportError as exc:
        _emit_result(
            {
                "status": "error",
                "error": (
                    "openhands-sdk not importable. Install with: "
                    "`pip install -r openhands-runner/requirements.txt`."
                ),
                "trace": str(exc),
            }
        )
        sys.exit(2)

    # OpenHands SDK + ACP transport write decorative output (banner, panels,
    # finish messages) to stdout. Capture all of it so the only lines on
    # actual stdout are our prefixed `event:` / `result:` lines.
    captured_stdout = io.StringIO()
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

    def on_event(event: Event) -> None:
        """Forward each SDK Event to the TS caller as a prefixed JSON line.

        Runs in whatever thread the SDK invokes callbacks on; print + flush
        are thread-safe at the line granularity we care about. Swallow our
        own exceptions — a bad event must not crash the agent loop.
        """
        try:
            payload = event.model_dump(mode="json")
        except Exception:
            # Some events may have unencodable fields in edge cases.
            payload = {"repr": repr(event)}
        try:
            _emit_event(
                {
                    "id": getattr(event, "id", None),
                    "type": event.__class__.__name__,
                    "source": str(getattr(event, "source", "")),
                    "ts": getattr(event, "timestamp", None) or time.time(),
                    "payload": payload,
                }
            )
        except Exception:
            # stdout broken? nothing else we can do; let the run continue.
            pass

    try:
        with contextlib.redirect_stdout(captured_stdout):
            agent = ACPAgent(
                acp_command=["claude-agent-acp"],
                acp_env={
                    "CLAUDE_CODE_EXECUTABLE": claude_exec,
                    "CLAUDE_CONFIG_DIR": claude_cfg,
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                },
                # Tier the agent's model; None → claude-agent-acp's default.
                acp_model=acp_model,
            )
            conversation = Conversation(
                agent=agent,
                workspace=workspace_dir,
                callbacks=[on_event],
            )
            conversation.send_message(wrapped_goal)
            conversation.run()
            agent.close()
    except Exception as exc:
        _emit_result(
            {
                "status": "error",
                "error": str(exc),
                "trace": traceback.format_exc(),
                "agent_output": captured_stdout.getvalue(),
            }
        )
        sys.exit(1)

    _emit_result(
        {
            "status": "ok",
            "workspace_dir": workspace_dir,
            "message": "OpenHands completed.",
            "agent_output": captured_stdout.getvalue(),
        }
    )


if __name__ == "__main__":
    main()
