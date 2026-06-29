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

import atexit
import contextlib
import glob as _glob
import io
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

# Wall-clock cap for the verify gate subprocess so a hung test suite can't hang
# the task forever (the agent's own wall-clock guard is separate — improvement #3).
_VERIFY_TIMEOUT_S = int(os.environ.get("DEVCLAW_VERIFY_TIMEOUT_S", "900"))

# Skill bundle baked into the sandbox image at /opt/devclaw/skills/. Layout:
#   _common.md          → always prepended
#   _writes-code/*.md   → for kinds that write code (implement_feature, fix_bug)
#   <kind>/*.md         → kind-specific (review_repository, onboard, …)
# Files inside a tier are sorted lexicographically so a leading number controls
# order. Repo-specific guidance still lives in the target repo's AGENTS.md — the
# skills carry devclaw's cross-repo doctrine; AGENTS.md carries this repo's facts.
_SKILLS_DIR = os.environ.get("DEVCLAW_SKILLS_DIR", "/opt/devclaw/skills")
_HOOKS_DIR = os.environ.get("DEVCLAW_HOOKS_DIR", "/opt/devclaw/hooks")
_WRITES_CODE_KINDS = {"implement_feature", "fix_bug"}
_HOOK_TIMEOUT_S = int(os.environ.get("DEVCLAW_HOOK_TIMEOUT_S", "30"))


def _read_skill(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _skill_paths_for_root(root: str, kind: str) -> list[str]:
    """Return the ordered list of skill files under a given root, for a kind.

    Order: ``_common.md`` → ``_writes-code/*.md`` (only for kinds that write
    code) → ``<kind>/*.md`` → any other ``*.md`` at the root (catch-all for
    per-repo observation files that don't fit a tier, e.g. closeloop's
    ``frontend-structure.md``). Files within a tier are sorted
    lexicographically so a leading number controls order. Missing tiers are
    silently skipped — a partial layout can't crash the runner.
    """
    paths: list[str] = []
    common = os.path.join(root, "_common.md")
    if os.path.exists(common):
        paths.append(common)
    if kind in _WRITES_CODE_KINDS:
        paths.extend(sorted(_glob.glob(os.path.join(root, "_writes-code", "*.md"))))
    paths.extend(sorted(_glob.glob(os.path.join(root, kind, "*.md"))))
    # Catch-all: any *.md at the skill root not already picked up. Useful for
    # per-repo observation files that aren't per-kind (the project's overall
    # state, e.g. "App.tsx is a known monolith"). _common.md is already
    # included above; skip it here to avoid double-loading.
    already = set(paths)
    for path in sorted(_glob.glob(os.path.join(root, "*.md"))):
        if path not in already and os.path.basename(path) != "_common.md":
            paths.append(path)
    return paths


def _load_skills(kind: str, workspace_dir: str | None = None) -> str:
    """Concatenate the skill bundle for a given task kind.

    Loads universal devclaw skills from ``/opt/devclaw/skills/`` (baked into
    the image), then appends per-repo skills from ``<workspace>/.agent/skills/``
    if a workspace is provided. The per-repo layer carries project-specific
    observations the universal skills can't (e.g. "App.tsx is a 1827-line
    monolith") and evolves at the repo's pace — symmetric with the per-repo
    hook discovery in :func:`_run_hook`. Universal skills come FIRST so the
    repo can lean on doctrine the agent already has.

    Empty paths and missing files are tolerated; at worst the agent just
    gets less briefing.
    """
    paths: list[str] = _skill_paths_for_root(_SKILLS_DIR, kind)
    if workspace_dir:
        paths.extend(_skill_paths_for_root(
            os.path.join(workspace_dir, ".agent", "skills"), kind,
        ))
    blocks = [b for b in (_read_skill(p) for p in paths) if b]
    return "\n\n---\n\n".join(blocks)


def _run_one_hook(path: str, args: tuple[str, ...]) -> tuple[bool, str]:
    """Run a single hook script (best-effort). Returns (ran, captured_output)."""
    if not os.path.exists(path):
        return False, ""
    try:
        proc = subprocess.run(
            ["bash", path, *args],
            capture_output=True,
            text=True,
            timeout=_HOOK_TIMEOUT_S,
        )
        return True, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return True, f"hook timed out after {_HOOK_TIMEOUT_S}s"
    except OSError as exc:
        return True, f"hook failed to start: {exc}"


def _run_hook(name: str, *args: str) -> list[str]:
    """Run the universal devclaw hook then the per-repo hook (if either exists).

    Universal hooks live in /opt/devclaw/hooks/ (baked into the sandbox image,
    devclaw-owned). Per-repo hooks live in <workspace>/.agent/hooks/ (project-
    owned, evolves at the repo's pace). Both contribute to the warnings list
    with a tagged prefix so the goal layer can tell them apart.

    Returns a list of warning lines (possibly empty). Hook failures are NOT
    fatal — they're advisory; the verify gate is the source of truth.
    """
    warnings: list[str] = []
    # Universal: devclaw-owned doctrine baked into the image.
    universal_path = os.path.join(_HOOKS_DIR, f"{name}.sh")
    ran, out = _run_one_hook(universal_path, args)
    if ran and out:
        warnings.append(f"[{name}] {out}")
    # Per-repo: project-owned, lives in the workspace alongside AGENTS.md.
    # args[0] is workspace_dir by convention; if no args we can't locate it.
    if args:
        repo_path = os.path.join(args[0], ".agent", "hooks", f"{name}.sh")
        ran, out = _run_one_hook(repo_path, args)
        if ran and out:
            warnings.append(f"[{name}:repo] {out}")
    return warnings


# (Legacy embedded preambles — kept only as the in-process fallback when the
# baked skill dir is missing. The sandbox image's /opt/devclaw/skills/ is the
# canonical source; these strings exist so devclaw still runs in degraded mode
# without it.)
_CONTEXT_PREAMBLE = (
    "You are working in the repository in your current working directory. Before "
    "changing anything, get your bearings: read the project's own guide if present "
    "(AGENTS.md, CLAUDE.md, or README.md in the repo root) and the existing code "
    "around what you're touching, so your change matches the project's conventions "
    "and structure. Do NOT assume the existing code is good — assess what you touch: "
    "if it's poorly structured, buggy, or has weak/missing tests, that is part of "
    "the job, not a pattern to copy. Follow the project's stated conventions and "
    "sound engineering over blindly mimicking bad surrounding code, and note in your "
    "summary anything pre-existing you had to work around or that needs follow-up. "
    "AGENTS.md in the repo root is the project's ACCUMULATED AGENT HARNESS — read it "
    "FIRST so you don't re-derive what's already known (stack, how to run/test, "
    "layout, conventions, key decisions, gotchas, reusable patterns). As part of "
    "this change, KEEP IT CURRENT: if it's missing, create it; if you learned or "
    "decided something a future task would otherwise have to re-reason, record it "
    "there concisely. It is the memory that saves the next task from re-thinking "
    "the same topics — treat maintaining it as part of the work, not optional."
)
# The engineer writes its OWN commit, the way a developer does — so the delivered
# PR's title/branch/body describe WHAT CHANGED, not the ticket instruction. devclaw
# derives the branch + PR from this commit, so a clean conventional-commit message
# here is what makes the history readable. (Don't push or open a PR — devclaw does.)
_COMMIT_CODA = (
    "Finally, COMMIT your change yourself with a clean conventional-commit message: "
    "a concise subject line in the form `type(scope): what changed` (type = feat / "
    "fix / refactor / test / docs / chore; imperative, ≤ ~70 chars, describing the "
    "CHANGE — not the task you were given), then a blank line, then a short body "
    "explaining WHY and how you verified it. Make ONE commit for the whole change "
    "(stage everything, including new files). Do NOT push and do NOT open a pull "
    "request — devclaw delivers your commit as a branch + PR."
)
# The code-quality bar. Without it the agent optimizes for the ONE thing it's
# told to satisfy — a green test suite — and ships "a working version": logic
# inlined wherever instead of where it belongs, happy-path-only tests, and even
# dead/no-op code that passes because nothing exercises it (live-observed: a
# `Directory.Enumerate(...).Take(0).Count()` accessibility check that enumerates
# nothing and never throws — green, but meaningless). The gate proves "didn't
# break + happy path works," not "good code." This brief carries the quality
# expectation devclaw (the PM) owes the engineer; repo-specific conventions still
# come from the repo's own AGENTS.md (read via the preamble).
_QUALITY_BAR = (
    "You are a senior software engineer working on this codebase. Code quality is "
    "part of your output — not just whether tests pass. Hold yourself to a production "
    "code-quality bar: code you would approve in a thorough code review.\n\n"
    "Before editing a file, read it and the surrounding folder. Form an opinion as a "
    "senior engineer would: is this a coherent unit or a god object mixing many "
    "concerns? Are responsibilities split where they belong, or piled into one? If "
    "you see code smells — god objects, mixed concerns, repeated patterns, catch-all "
    "spec files, missing abstractions — refactor first, then add. Sound engineering "
    "beats matching the existing pattern when the existing pattern is bad; match the "
    "standard of a well-maintained open-source library, not the local habit if the "
    "local habit is rotten.\n\n"
    "Producing the change: put new code where it BELONGS — sometimes that's the "
    "existing location, sometimes a better location you create and migrate to (note "
    "structural moves in your summary). Follow existing style and naming when sound, "
    "propose better when not. Write NO dead, placeholder, or no-op code — every line "
    "must do real work; a disabled button + expect(visible) is not implementation, "
    "it's a stub in disguise. Handle real edge and error cases. Tests must genuinely "
    "exercise behaviour, never weakened or deleted to go green.\n\n"
    "Before finishing, re-read your own diff with the senior engineer eye. Two "
    "questions: (1) does it work? tests pass, behaviour correct, edges handled. "
    "(2) is the codebase healthier than before this change, or worse? A passing test "
    "suite is necessary but NOT sufficient. If either answer is no, fix it."
)
_VERIFY_CODA = (
    "Keep the change focused. Refactoring WHAT YOU TOUCH is part of the change — if "
    "you edit a god object to add a feature, splitting it is the work, not unrelated. "
    "The line is between refactors that SUPPORT the change (in scope) and refactors "
    "of code you didn't otherwise need to touch (out of scope). When done, VERIFY "
    "your work with the project's OWN tools, and iterate until they pass: run the "
    "test/build command AND the linter, formatter, and type-checker if the repo has "
    "any (look in package.json scripts, pyproject.toml / setup.cfg, Makefile, "
    ".pre-commit-config.yaml, or configs like .eslintrc / ruff / mypy / tsconfig) — "
    "fix everything they flag, not only failing tests. Finish with a short summary "
    "of what you changed and the checks you ran (tests + lint + types) to verify it."
)

_KIND_WRAPPERS = {
    "implement_feature": (
        f"{_CONTEXT_PREAMBLE}\n\n{_QUALITY_BAR}\n\n{_VERIFY_CODA}\n\n{_COMMIT_CODA}\n\n"
        f"Feature to implement:\n{{goal}}"
    ),
    "fix_bug": (
        f"{_CONTEXT_PREAMBLE} Make the smallest change that fixes the bug.\n\n"
        f"{_QUALITY_BAR}\n\n{_VERIFY_CODA}\n\n{_COMMIT_CODA}\n\nBug description:\n{{goal}}"
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


def _wrap_goal(kind: str, goal: str, workspace_dir: str | None = None) -> str:
    """Skills prepended, then the goal under a clear marker.

    Loads universal skills from /opt/devclaw/skills/ plus per-repo skills from
    ``<workspace>/.agent/skills/`` when ``workspace_dir`` is provided. Falls
    back to the legacy embedded ``_KIND_WRAPPERS`` only when no skill files at
    all are found (host-side dev, fresh image without skills/ baked in, AND
    the repo also has no .agent/skills). Once the sandbox image ships skills,
    that fallback is dead path.
    """
    skills = _load_skills(kind, workspace_dir=workspace_dir)
    if skills:
        return f"{skills}\n\n---\n\n## Goal\n\n{goal}"
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
    # without a custom system prompt. Skills now live in /opt/devclaw/skills/
    # and are loaded per-kind by _wrap_goal.
    wrapped_goal = _wrap_goal(kind, goal, workspace_dir=workspace_dir)

    os.makedirs(workspace_dir, exist_ok=True)

    # Drop the sandbox-only MCP config into the workspace so claude auto-
    # discovers it at project scope. The image bakes /opt/devclaw/sandbox-mcp.json
    # (Playwright MCP only); we don't mount the host's mcpServers because the
    # sandcastle allowlist deliberately excludes them. Skip if the workspace
    # already has its own .mcp.json so a project can override. Mark the file
    # as locally-ignored via .git/info/exclude so the agent's `git add .` can't
    # accidentally commit it; we also remove it in the finally block below in
    # case the workspace isn't a git repo.
    _baked_mcp = "/opt/devclaw/sandbox-mcp.json"
    _workspace_mcp = os.path.join(workspace_dir, ".mcp.json")
    _mcp_dropped = False
    if os.path.exists(_baked_mcp) and not os.path.exists(_workspace_mcp):
        try:
            shutil.copyfile(_baked_mcp, _workspace_mcp)
            _mcp_dropped = True
        except OSError:
            # Best-effort: a read-only workspace mount shouldn't fail the run.
            pass
    if _mcp_dropped:
        _exclude = os.path.join(workspace_dir, ".git", "info", "exclude")
        if os.path.isdir(os.path.dirname(_exclude)):
            try:
                with open(_exclude, "a", encoding="utf-8") as fh:
                    fh.write("\n.mcp.json\n")
            except OSError:
                pass

        def _cleanup_mcp() -> None:
            try:
                os.remove(_workspace_mcp)
            except OSError:
                pass

        atexit.register(_cleanup_mcp)

    # Hook warnings accumulated across pre/post hooks. Surfaced in the result
    # payload so the goal layer's evaluator can read them (e.g. "you added
    # e2e tests but verify_cmd does not run them"). Hooks are best-effort —
    # their warnings are advisory, the verify gate is the source of truth.
    # Pre-run hook fires AFTER the MCP config drop so it sees the final
    # workspace state. _run_hook fires both the universal hook AND any per-repo
    # hook in <workspace>/.agent/hooks/, returning a list of tagged warnings.
    task_id = str(req.get("task_id") or "")
    hook_warnings: list[str] = []
    hook_warnings.extend(_run_hook("pre-run", workspace_dir, kind, task_id))

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
        err_payload = {
            "status": "error",
            "error": str(exc),
            "trace": traceback.format_exc(),
            "agent_output": captured_stdout.getvalue(),
        }
        if hook_warnings:
            err_payload["hook_warnings"] = hook_warnings
        _emit_result(err_payload)
        sys.exit(1)

    # Post-run hook: mechanical checks against what the agent shipped (e.g.
    # "you added browser tests but verify_cmd is still pytest-only"). Runs
    # BEFORE the verify gate so the hook can pass verify_cmd to its diff-aware
    # checks and so its warnings ride alongside the gate verdict in the result.
    hook_warnings.extend(
        _run_hook("post-run", workspace_dir, kind, task_id, verify_cmd or "")
    )

    result_payload = {
        "status": "ok",
        "workspace_dir": workspace_dir,
        "message": "OpenHands completed.",
        "agent_output": captured_stdout.getvalue(),
    }
    if hook_warnings:
        result_payload["hook_warnings"] = hook_warnings

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
