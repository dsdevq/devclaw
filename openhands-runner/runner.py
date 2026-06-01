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
import subprocess
import sys
import time
import traceback

# Wall-clock cap for the verify gate subprocess so a hung test suite can't hang
# the task forever (the agent's own wall-clock guard is separate — improvement #3).
_VERIFY_TIMEOUT_S = int(os.environ.get("DEVCLAW_VERIFY_TIMEOUT_S", "900"))


# Operating instructions prepended to the user's goal before it's sent to the ACP
# agent (Claude Code). Cheap behavioral scaffolding: the agent is capable, but a
# RAW goal made it start blind on an existing repo — it didn't read the project's
# own conventions and didn't verify its own work. This briefs it on the repo and
# tells it to self-verify. (Shaped by OpenHands' prompting guidance: concrete,
# location-aware, scoped, run the tests.) devclaw's own verify gate still
# double-checks the result — this is the engineer self-checking, not the gate.
_CONTEXT_PREAMBLE = (
    "You are working in the repository in your current working directory. Before "
    "changing anything, get your bearings: read the project's own guide if present "
    "(AGENTS.md, CLAUDE.md, or README.md in the repo root) and the existing code "
    "around what you're touching, so your change matches the project's conventions "
    "and structure."
)
_VERIFY_CODA = (
    "Keep the change focused — do not refactor unrelated code. When done, VERIFY "
    "your work: run the project's existing test/build command and iterate until it "
    "passes. Finish with a short summary of what you changed and how you verified it."
)

_KIND_WRAPPERS = {
    "implement_feature": (
        f"{_CONTEXT_PREAMBLE}\n\n{_VERIFY_CODA}\n\nFeature to implement:\n{{goal}}"
    ),
    "fix_bug": (
        f"{_CONTEXT_PREAMBLE} Make the smallest change that fixes the bug.\n\n"
        f"{_VERIFY_CODA}\n\nBug description:\n{{goal}}"
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
    # Onboarding: analyse the repo and produce a DRAFT AGENTS.md so future tasks
    # start informed (1b already reads AGENTS.md/CLAUDE.md/README if present —
    # this generates that file for repos that lack one). Comprehension only —
    # "what is", NOT direction or a decision log (kept separate per the operating
    # model). Read-only EXCEPT the single AGENTS.md you write. Human-in-the-loop:
    # the draft is surfaced for review (git working tree + the summary), and is
    # NOT authoritative until reviewed — so when an AGENTS.md already exists we
    # validate it against the real repo and keep what's correct rather than
    # blindly clobbering it.
    "onboard": (
        "You are ONBOARDING this repository: produce a comprehension guide so a "
        "future engineer (and an automated agent) can start work already "
        "informed. Inspect the repo READ ONLY — read files and run read-only "
        "inspection commands (ls, cat, grep, git log, find, reading config/"
        "manifest/lockfiles, etc.). Do NOT modify, create, or delete ANY file "
        "EXCEPT the single AGENTS.md described below; in particular do not change "
        "any source, build, or config file.\n\n"
        "Write your findings to AGENTS.md in the repository root. This is "
        "COMPREHENSION ONLY — describe WHAT IS, not what should change: \n"
        "  - Stack & languages (frameworks, runtimes, key dependencies + versions)\n"
        "  - Layout (the important directories/modules and what each is for)\n"
        "  - How to build, run, and TEST it — the exact commands, and call out "
        "the single command that should be used as the verification gate "
        "(what proves a change is good)\n"
        "  - Conventions (code style, naming, branching, commit/PR norms you can "
        "infer from the repo)\n"
        "  - Setup prerequisites and gotchas (toolchain versions, env vars, "
        "services, anything non-obvious that bites a newcomer)\n"
        "Do NOT include project direction, roadmap, a decision log, or opinions "
        "about what to build next — that is deliberately out of scope.\n\n"
        "If AGENTS.md does NOT already exist: create it, and put a one-line note "
        "at the very top marking it as a DRAFT generated by devclaw onboarding "
        "for human review. If AGENTS.md ALREADY exists: do not blindly overwrite "
        "it — validate each part against the actual repository, KEEP everything "
        "that is still accurate, and only correct or fill in what is wrong, "
        "stale, or missing (preserving the existing structure). If the existing "
        "AGENTS.md is already fully accurate, leave it unchanged.\n\n"
        "End with a short summary to STDOUT in your final message: whether you "
        "created or updated (or left unchanged) AGENTS.md, and the key facts you "
        "captured. Optional extra focus for this onboarding (if any):\n{goal}"
    ),
}


def _wrap_goal(kind: str, goal: str) -> str:
    template = _KIND_WRAPPERS.get(kind, _KIND_WRAPPERS["implement_feature"])
    return template.format(goal=goal)


def _run_verify(cmd: str, workspace_dir: str, timeout: int = _VERIFY_TIMEOUT_S) -> dict:
    """Run the verify gate in the workspace AFTER the agent finishes and return a
    verdict. The agent saying "done" isn't trusted — the project's own
    test/build command exiting 0 is what "done" means. Run via the shell so a
    full command line works ("npm run build && npm run test:ci"); combined
    stdout+stderr, tail-truncated. Never raises — a crash/timeout is a failed
    gate, not a runner crash."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.output or "") + (exc.stderr or "")
        return {
            "ran": True, "cmd": cmd, "passed": False, "exit_code": None,
            "timed_out": True, "output": partial[-4000:],
        }
    except OSError as exc:
        return {
            "ran": True, "cmd": cmd, "passed": False, "exit_code": None,
            "timed_out": False, "output": f"failed to run verify command: {exc}",
        }
    combined = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ran": True, "cmd": cmd, "passed": proc.returncode == 0,
        "exit_code": proc.returncode, "timed_out": False, "output": combined[-4000:],
    }


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
    verify_cmd = req.get("verify_cmd")  # optional gate run after the agent finishes
    if not workspace_dir or not goal:
        _emit_result(
            {
                "status": "error",
                "error": "request must include workspace_dir and goal",
            }
        )
        sys.exit(2)

    if kind not in ("implement_feature", "fix_bug", "review_repository", "onboard"):
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

    result_payload = {
        "status": "ok",
        "workspace_dir": workspace_dir,
        "message": "OpenHands completed.",
        "agent_output": captured_stdout.getvalue(),
    }

    # Verify gate: the agent loop finished, but "done" means the project's own
    # test/build command passes — run it now and attach the verdict. The host
    # (TaskQueue) decides done-vs-failed from `verify.passed`; here we just run it
    # and report. Emitted as an event too so it shows in the live stream.
    if verify_cmd:
        verify = _run_verify(verify_cmd, workspace_dir)
        result_payload["verify"] = verify
        _emit_event(
            {
                "id": "verify",
                "type": "VerifyResult",
                "source": "devclaw",
                "ts": time.time(),
                "payload": {
                    "cmd": verify["cmd"],
                    "passed": verify["passed"],
                    "exit_code": verify["exit_code"],
                    "timed_out": verify["timed_out"],
                },
            }
        )

    _emit_result(result_payload)


if __name__ == "__main__":
    main()
