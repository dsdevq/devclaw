"""
DevClaw v2 — OpenHands runner.

Spawned as a subprocess by the TypeScript MCP server when an `implement_feature`
MCP call lands. Reads a single JSON request from argv[1] (so command lines stay
readable) and writes a single JSON response to stdout.

Request shape:
    {"workspace_dir": "/abs/path", "goal": "natural-language task"}

Response shape (success):
    {"status": "ok", "final_message": "...", "tool_calls": N}

Response shape (failure):
    {"status": "error", "error": "...", "trace": "..."}

Authentication: relies on Claude Code OAuth session via CLAUDE_CODE_EXECUTABLE
+ CLAUDE_CONFIG_DIR env vars (validated end-to-end 2026-05-25 smoke test).
No ANTHROPIC_API_KEY required or accepted.
"""

import contextlib
import io
import json
import os
import sys
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


def _refuse_api_key() -> None:
    """Refuse to run if an API key snuck into the env — preserves the
    Pro-subscription cost model (memory: pro-subscription-is-the-design)."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": (
                            f"{var} is set in the environment. DevClaw v2 runs "
                            "exclusively through Claude Code OAuth — refusing to "
                            "spend metered credits."
                        ),
                    }
                )
            )
            sys.exit(2)


def main() -> None:
    _refuse_api_key()

    if len(sys.argv) != 2:
        print(json.dumps({"status": "error", "error": "expected one JSON arg"}))
        sys.exit(2)

    try:
        req = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "error": f"invalid JSON: {exc}"}))
        sys.exit(2)

    workspace_dir = req.get("workspace_dir")
    goal = req.get("goal")
    kind = req.get("kind", "implement_feature")
    if not workspace_dir or not goal:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "request must include workspace_dir and goal",
                }
            )
        )
        sys.exit(2)

    if kind not in ("implement_feature", "fix_bug", "review_repository"):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": f"unknown kind: {kind}",
                }
            )
        )
        sys.exit(2)

    # Wrap the user's goal with kind-specific operating instructions. The
    # OpenHands ACP-driven Claude session reads this as the user message,
    # so prepending instructions here is the cheapest way to bias behavior
    # without a custom system prompt.
    wrapped_goal = _wrap_goal(kind, goal)

    os.makedirs(workspace_dir, exist_ok=True)

    claude_exec = os.environ.get("CLAUDE_CODE_EXECUTABLE") or "/home/dsdevqq/.local/bin/claude"
    claude_cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")

    try:
        from openhands.sdk.agent import ACPAgent
        from openhands.sdk.conversation import Conversation
    except ImportError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": (
                        "openhands-sdk not importable. Install with: "
                        "`npm run python:install` from the v2/ directory."
                    ),
                    "trace": str(exc),
                }
            )
        )
        sys.exit(2)

    # OpenHands SDK and its ACP transport write decorative output (banner,
    # live tool-call panels, finish messages) to stdout. Capture all of that
    # so our final JSON line is the only thing on actual stdout — the
    # TypeScript caller needs clean JSON to parse the result.
    captured_stdout = io.StringIO()
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

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
            )
            conversation = Conversation(agent=agent, workspace=workspace_dir)
            conversation.send_message(wrapped_goal)
            conversation.run()
            agent.close()
    except Exception as exc:
        # On failure, surface the captured agent output as the trace —
        # otherwise debugging is blind.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "trace": traceback.format_exc(),
                    "agent_output": captured_stdout.getvalue(),
                }
            )
        )
        sys.exit(1)

    # Conversation doesn't expose a clean "final message" accessor across SDK
    # versions; for slice 1 we report success + workspace location and pass
    # the captured agent output through so the caller can inspect/log it.
    print(
        json.dumps(
            {
                "status": "ok",
                "workspace_dir": workspace_dir,
                "message": (
                    "OpenHands completed. Inspect workspace_dir for the resulting files; "
                    "richer result extraction lands in slice 2."
                ),
                "agent_output": captured_stdout.getvalue(),
            }
        )
    )


if __name__ == "__main__":
    main()
