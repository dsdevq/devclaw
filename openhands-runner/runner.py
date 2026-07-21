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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

# Wall-clock cap for the verify gate subprocess so a hung test suite can't hang
# the task forever (the agent's own wall-clock guard is separate — improvement #3).
_VERIFY_TIMEOUT_S = int(os.environ.get("DEVCLAW_VERIFY_TIMEOUT_S", "900"))

# Skill bundle baked into the sandbox image at /opt/devclaw/skills/. Layout:
#   _common.md          → always prepended
#   _writes-code/*.md   → for kinds that write code (implement_feature, fix_bug)
#   <kind>/*.md         → kind-specific (review_repository, onboard, …)
#   craft/*.md          → self-selected reference (NOT concatenated) — how-to
#                         guides the agent discovers by `ls`/`cat` when a task
#                         calls for them (frontend-design, playwright). Kept out
#                         of the always-on brief on purpose: the globs below only
#                         reach _common/_writes-code/<kind>/root-level *.md, so a
#                         sibling subdir like craft/ is never pulled in.
# The always-on tiers are DOCTRINE (follow every task); craft/ is CRAFT (read
# when relevant). Files inside a tier are sorted lexicographically so a leading
# number controls order. Repo-specific guidance still lives in the target repo's
# AGENTS.md — the skills carry devclaw's cross-repo doctrine; AGENTS.md carries
# this repo's facts.
_SKILLS_DIR = os.environ.get("DEVCLAW_SKILLS_DIR", "/opt/devclaw/skills")
_HOOKS_DIR = os.environ.get("DEVCLAW_HOOKS_DIR", "/opt/devclaw/hooks")
_WRITES_CODE_KINDS = {"implement_feature", "fix_bug"}
_HOOK_TIMEOUT_S = 30


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
# The structured hand-back the engineer owes devclaw at the END of a code task.
# Replaces the old bare "say DONE": the goal layer (and the direction evaluator
# that reads deliveries.md next tick) needs a legible, parseable account of what
# actually shipped — what changed, what was verified, which acceptance criteria
# are met, what's still open — not a one-word signal. Rendered LAST so it's the
# final thing the engineer reads before finishing. It reports the OUTCOME of the
# work and never prescribes HOW to do it, so it does not fight _QUALITY_BAR's
# "form your own opinion as a senior engineer." Vendor-neutral plain markdown —
# the model-agnostic worker layer carries no vendor tool-wiring.
_RETURN_CONTRACT = (
    "## When you finish — hand back a structured summary\n\n"
    "End your final message with a hand-back in exactly this shape — one line "
    "per field, plain text, no code fence — so devclaw can read your result "
    "without guessing:\n\n"
    "STATUS: DONE  — or  BLOCKED: <one-line reason>  if you could not finish.\n"
    "CHANGED: the files/areas you changed, one clause each, and what each change does.\n"
    "VERIFIED: the checks you ACTUALLY ran and their result — tests, lint, "
    "type-check, build (name the commands).\n"
    "ACCEPTANCE: for each acceptance criterion stated in the Goal, whether it is "
    "met and the evidence; write 'none stated' if the Goal listed none.\n"
    "FOLLOW-UPS: anything you had to work around, left unfinished, or that needs "
    "a human — or 'none'.\n"
    "REPO NOTES: durable repo-level facts a future engineer on a DIFFERENT task "
    "in this repository would need — build/test quirks, non-obvious commands, "
    "environment gotchas — as short semicolon-separated clauses, or 'none'. "
    "Never task-specific detail (that belongs in CHANGED), never speculation.\n\n"
    "Report only checks you truly ran, not ones you intended to. If you write "
    "BLOCKED, still fill CHANGED / VERIFIED / ACCEPTANCE with how far you got."
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
        "You are ONBOARDING this repository: produce a documentation set so a "
        "future engineer (and an automated agent) can start work already "
        "informed. Inspect the repo READ ONLY — read files and run read-only "
        "inspection commands (ls, cat, grep, git log, find, reading config/"
        "manifest/lockfiles, etc.). Do NOT modify, create, or delete ANY file "
        "EXCEPT the four documents described below; in particular do not "
        "change any source, build, or config file.\n\n"
        "Produce FOUR docs in the repo root — each has one job, do not blur "
        "them:\n"
        "  1. AGENTS.md — agent-facing COMPREHENSION: stack, layout, build/run/"
        "test commands (call out the verify gate), conventions, prerequisites, "
        "gotchas. Describe WHAT IS.\n"
        "  2. README.md — human-facing intro: one-paragraph purpose, minimum "
        "quickstart commands, high-level pointer at the layout (link AGENTS.md "
        "for detail), one-line status.\n"
        "  3. ARCHITECTURE.md — component map, data flow, cross-cutting "
        "concerns, notable design decisions. Diagrams welcome (ASCII / mermaid); "
        "if you can't draw one, leave `<!-- diagram: ... -->` prose describing "
        "what should go there.\n"
        "  4. DECISIONS.md — ADR-style entries (date, title, context, decision, "
        "consequences, alternatives). Reconstruct from git log + code comments "
        "+ any prior docs. Mark reconstructed entries `(reconstructed; may need "
        "review)`. If nothing is inferrable, DECISIONS.md still exists with a "
        "header + a one-line 'no ADRs captured yet' note.\n\n"
        "Rules across all four:\n"
        "  - Each doc you CREATE gets a one-line DRAFT marker at the top for "
        "human review.\n"
        "  - If a doc ALREADY exists and is substantive, do NOT clobber — "
        "validate each part against the repo, keep what's accurate, only "
        "correct / fill missing bits.\n"
        "  - Boundary discipline: don't put ADR reasoning in README, don't put "
        "quickstart in ARCHITECTURE, don't put decision rationale in AGENTS.md. "
        "Cross-link instead.\n"
        "  - Read-only for everything else in the repo.\n\n"
        "End with a short summary to STDOUT: for each of the four docs, whether "
        "you CREATED / UPDATED / LEFT UNCHANGED it, plus two or three "
        "load-bearing facts you captured per doc. Optional extra focus for this "
        "onboarding (if any):\n{goal}"
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
        brief = f"{skills}\n\n---\n\n## Goal\n\n{goal}"
        effective_kind = kind
    else:
        # An unknown kind falls back to the implement_feature template — so it
        # must also inherit implement_feature's return contract (keeps the
        # unknown-kind == implement_feature equivalence the fallback promises).
        effective_kind = kind if kind in _KIND_WRAPPERS else "implement_feature"
        brief = _KIND_WRAPPERS[effective_kind].format(goal=goal)
    # Structured return contract — code-writing kinds only. review_repository and
    # onboard already carry their own report contract (a written review / doc
    # set), so appending the code hand-back would fight it. Rendered LAST so the
    # engineer reads it right before finishing.
    if effective_kind in _WRITES_CODE_KINDS:
        brief = f"{brief}\n\n---\n\n{_RETURN_CONTRACT}"
    return brief


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


#: Where the browser-E2E gate's proof-of-execution lands. The runner points
#: Playwright's JSON reporter here (via PLAYWRIGHT_JSON_OUTPUT_NAME) so a browser
#: run's real pass/fail counts survive to the host — the 4 KB-truncated verify
#: `output` can't carry them. A devclaw-owned path (not the project's default)
#: so it reflects THIS run, never a stale artifact from a prior attempt.
_BROWSER_REPORT_REL = os.path.join(".devclaw", "playwright-report.json")


def _read_browser_report(workspace_dir: str) -> "dict | None":
    """Parse the Playwright JSON reporter artifact the verify gate produced (if
    any) into the compact {expected, unexpected, flaky, skipped} summary the
    host browser-gate keys off. Best-effort: a missing/garbled artifact returns
    None, which the host reads as "no browser run" — fail-closed for a frontend
    change, never a false pass."""
    path = os.path.join(workspace_dir, _BROWSER_REPORT_REL)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    stats = data.get("stats") if isinstance(data, dict) else None
    if not isinstance(stats, dict):
        return None
    if not any(k in stats for k in ("expected", "unexpected", "flaky", "skipped")):
        return None
    return {
        "expected": int(stats.get("expected", 0) or 0),
        "unexpected": int(stats.get("unexpected", 0) or 0),
        "flaky": int(stats.get("flaky", 0) or 0),
        "skipped": int(stats.get("skipped", 0) or 0),
    }


# --- usage-limit detection ---------------------------------------------------
# Vendored subset — keep in sync with devclaw/loom/limits.py. The runner runs
# inside the sandbox WITHOUT the devclaw package installed, so it cannot import
# the canonical classifier; these are verbatim copies of its AUTH/QUOTA/RATE
# patterns and the relative retry-after parser. Purpose: when the agent loop
# dies on a clear model usage/rate limit, surface it STRUCTURALLY as
# status="rate_limited" (+ retry_after seconds when the text states one) so the
# host pauses instead of retry-burning quota. This is belt on top of suspenders:
# anything ambiguous stays status="error" and the host-side regex fallback
# (loom/limits.classify_failure) still sees the original text — false negatives
# are fine, false positives are not.

# AUTH: never tagged rate_limited HERE — auth text flows through as a plain
# error so the HOST classifier (loom/limits.py) sees the original wording and
# routes it onto the AUTH pause path (fixed re-probe + actionable owner ping,
# 2026-07-20 night incident). Checked FIRST, mirroring limits.py's priority
# order; keep the pattern in sync with limits.py's _AUTH.
_LIMIT_AUTH = re.compile(
    r"\b401\b|invalid authentication|failed to authenticate|unauthor|"
    r"authentication[ _]required|"
    r"authentication_error|oauth.*(expired|invalid)|please run /login",
    re.IGNORECASE,
)
# QUOTA: the longer "you're out for a while" caps (Claude Pro/Max usage limits).
_LIMIT_QUOTA = re.compile(
    r"usage limit|weekly limit|quota|out of (extra )?usage|ran out of \w*\s*usage|"
    r"limit reached|reached your (usage|plan) limit|you'?ve reached|"
    r"plan limit|insufficient_quota|credit balance|"
    r"session limit|hit your [\w ]{0,16}limit",
    re.IGNORECASE,
)
# RATE: short-term throttling (per-min / 5-hour) + HTTP 429.
_LIMIT_RATE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|5[ -]?hour limit|"
    r"slow down|requests per",
    re.IGNORECASE,
)

_LIMIT_UNITS = {"s": 1, "m": 60, "h": 3600}

# "try again in 5 minutes", "reset in 10m", "wait 2 hours" (number + unit) …
_LIMIT_RETRY_AFTER_UNIT = re.compile(
    r"(?:retry[- ]after|try again in|reset[s]? in(?: about)?|wait)\D{0,8}?"
    r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|h|m|s)\b",
    re.IGNORECASE,
)
# … and the bare HTTP header form "Retry-After: 30" (seconds, no unit word).
_LIMIT_RETRY_AFTER_HEADER = re.compile(r"retry[- ]after:?\s*(\d+)\b", re.IGNORECASE)


def _parse_retry_after(text: str) -> int | None:
    """Best-effort parse of a stated reset delay → seconds. None if not stated.
    (Absolute reset *times* like 'resets at 10pm' are intentionally not parsed —
    that needs a clock/timezone; the host applies its default backoff instead.)"""
    t = text or ""
    m = _LIMIT_RETRY_AFTER_UNIT.search(t)
    if m:
        return int(m.group(1)) * _LIMIT_UNITS[m.group(2)[0].lower()]
    m = _LIMIT_RETRY_AFTER_HEADER.search(t)
    if m:
        return int(m.group(1))  # bare Retry-After is seconds
    return None


def _detect_usage_limit(text: str | None) -> tuple[bool, int | None]:
    """Is this error text CLEARLY a model usage/rate limit? → (matched, retry_after_s).

    Conservative on purpose: auth-shaped text is never a limit (waiting can't fix
    an expired login), and anything not matching the vendored QUOTA/RATE wording
    returns (False, None) so it flows through as a plain error for the host regex
    to classify. retry_after is None when the text states no relative delay.
    """
    t = text or ""
    if _LIMIT_AUTH.search(t):
        return False, None
    if _LIMIT_QUOTA.search(t) or _LIMIT_RATE.search(t):
        return True, _parse_retry_after(t)
    return False, None


def _failure_result(error_text: str, **extra) -> dict:
    """Build the terminal result payload for an agent/conversation failure.

    A clear usage/rate-limit wording becomes status="rate_limited" with the
    ORIGINAL error text preserved (the host falls back to regex-classifying it
    when retry_after is absent) plus retry_after seconds or None. Everything
    else is the plain status="error" payload, byte-for-byte as before.
    """
    matched, retry_after = _detect_usage_limit(error_text)
    payload: dict = {"status": "error", "error": error_text, **extra}
    if matched:
        payload["status"] = "rate_limited"
        payload["retry_after"] = retry_after
    return payload


# The engineer's return-contract (_RETURN_CONTRACT) tells it to end its final
# message with a STATUS field whose value is either ``DONE`` or
# ``BLOCKED: <one-line reason>`` when it genuinely cannot finish (a missing
# capability, contradictory/impossible instructions). We honor that self-report
# as a first-class terminal status instead of letting it ride invisibly inside
# agent_output. Anchored to the START of a line (after an optional ``STATUS:``
# prefix and light markdown decoration like ``**``/``>``) and case-sensitive on
# the uppercase keyword the contract prescribes — so prose like "the run was
# blocked: on X but I fixed it" mid-sentence can't false-positive. Model-agnostic:
# it parses the agent's OWN plain-text hand-back, no vendor tool-wiring.
_BLOCKED_LINE_RE = re.compile(
    r"^[ \t>#*_-]*(?:STATUS:[ \t]*)?BLOCKED:[ \t]*(.*?)[ \t*_]*$",
    re.MULTILINE,
)


def _parse_blocked_reason(agent_message: str | None) -> str | None:
    """If the agent's final hand-back self-reports ``BLOCKED``, return the reason.

    Returns None when there is no blocked self-report (the normal path). The LAST
    matching line wins — the hand-back is rendered last, so a later BLOCKED line
    is the authoritative terminal signal. A reason that parses to empty still
    returns a non-None placeholder: an honest "I'm blocked" with no stated reason
    must still surface as a block, never be lost as "no reason ⇒ not blocked".
    """
    if not agent_message:
        return None
    matches = _BLOCKED_LINE_RE.findall(agent_message)
    if not matches:
        return None
    reason = matches[-1].strip().strip("*_ ").strip()
    return reason or "worker reported BLOCKED without a stated reason"


def _collect_usage(conversation) -> dict | None:
    """Best-effort per-task usage from the SDK's conversation stats.

    The ACP agent records each turn's token usage (and, when the CLI reports
    one, its cost) into the conversation's combined metrics; this flattens
    them into a plain dict that rides the result payload to the host. Usage
    is telemetry, never a gate: any schema drift or absent stats degrades to
    None (block omitted) rather than failing a finished run. All-zero stats
    also return None — an ACP server that doesn't report usage should read
    as "unknown", not "free".
    """
    try:
        metrics = conversation.conversation_stats.get_combined_metrics()
        tokens = getattr(metrics, "accumulated_token_usage", None)
        usage = {
            "input_tokens": int(getattr(tokens, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(tokens, "completion_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(tokens, "cache_read_tokens", 0) or 0),
            "cost_usd": round(float(getattr(metrics, "accumulated_cost", 0.0) or 0.0), 6),
        }
        return usage if any(usage.values()) else None
    except Exception:
        return None


# The hand-back's REPO NOTES field — durable repo-level facts for FUTURE tasks
# on the same repo (build/test quirks, non-obvious commands). Same parsing
# philosophy as the BLOCKED line: anchored to line start, light markdown
# decoration tolerated, the agent's OWN final message only, model-agnostic
# plain text. "none"/empty degrade to None — absence of notes is the normal
# case, never an error.
_REPO_NOTES_LINE_RE = re.compile(
    r"^[ \t>#*_-]*REPO NOTES:[ \t]*(.*?)[ \t*_]*$",
    re.MULTILINE,
)


def _parse_repo_notes(agent_message: str | None) -> str | None:
    """If the agent's final hand-back carries REPO NOTES, return them.

    The LAST matching line wins (mirrors ``_parse_blocked_reason``). A value
    of 'none' (any case) or empty returns None — the contract asks for 'none'
    explicitly, and an unfilled field must read as "nothing to record", not
    ride to the host as literal prose."""
    if not agent_message:
        return None
    matches = _REPO_NOTES_LINE_RE.findall(agent_message)
    if not matches:
        return None
    notes = matches[-1].strip().strip("*_ ").strip()
    if not notes or notes.lower().rstrip(".") == "none":
        return None
    return notes


def _agent_message_text(payload: dict) -> str:
    """Pull the plain text out of a MessageEvent payload (``model_dump`` shape).

    The OpenHands ``MessageEvent`` carries ``llm_message.content`` — a list of
    typed content parts; we concatenate the ``text`` parts. Defensive by design
    (best-effort, never raises): a schema drift degrades to ``""`` rather than
    crashing the event callback."""
    if not isinstance(payload, dict):
        return ""
    msg = payload.get("llm_message")
    if not isinstance(msg, dict):
        return ""
    parts: list[str] = []
    for item in msg.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


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


# The stock worker agent: Anthropic's ACP bridge for Claude Code. Kept as a
# tuple so the default can't be mutated by a caller.
_DEFAULT_ACP_COMMAND = ("claude-agent-acp",)


def _resolve_acp_command(req: dict) -> list[str]:
    """The ACP agent command the worker session runs on.

    Payload first (the host passes it exactly like `model` — host env vars do
    NOT cross the container boundary, so an env-only override would silently
    do nothing in the sandbox), then DEVCLAW_ACP_COMMAND for a manual
    `docker run` / host-engine run, then the claude-agent-acp default. A
    string spec is shlex-split so quoted arguments survive
    (`my-acp --profile 'a b'` → 3 argv entries).
    """
    raw = (req.get("acp_command") or os.environ.get("DEVCLAW_ACP_COMMAND") or "").strip()
    if not raw:
        return list(_DEFAULT_ACP_COMMAND)
    return shlex.split(raw)


# --- toolchain provisioning (ADR 0005) --------------------------------------
# The sandbox image ships NO language SDKs beyond python+node; the project's
# DECLARED toolchain is provisioned here, before the agent starts, and its
# environment exported into this process so the agent's shells AND the verify
# gate inherit it. Fail-closed: a declared toolchain that can't be provisioned
# settles the task `error` with a legible reason — silently running a .NET
# goal on a python+node box is exactly the silent degradation the hardening
# philosophy forbids.

# Wall-clock cap for `mise install`: the first task per toolchain version
# downloads whole SDKs (minutes); later tasks hit the per-project cache volume
# and finish in seconds.
_TOOLCHAIN_INSTALL_TIMEOUT_S = 900
#: mise-native declaration files — mise reads these directly, no translation.
_MISE_NATIVE_FILES = (".mise.toml", "mise.toml", ".tool-versions")


class ToolchainError(Exception):
    """A declared toolchain that cannot be provisioned. The message is the
    owner-actionable reason that rides the error result."""


def _translate_global_json(workspace_dir: str) -> dict:
    """``global.json`` ``sdk.version`` → ``{"dotnet": "<major.minor>"}``.

    Fuzzy major.minor, not the exact patch: global.json almost always rides
    rollForward semantics where any 9.0.x SDK satisfies "9.0.203", and exact
    patch pins frequently don't exist as installable versions. Present-but-
    unparseable raises (fail closed with the better message — dotnet itself
    would refuse the file later anyway); a global.json that pins no
    sdk.version declares nothing.
    """
    path = os.path.join(workspace_dir, "global.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolchainError(f"global.json is unreadable/invalid: {exc}")
    version = str((data.get("sdk") or {}).get("version") or "")
    m = re.match(r"(\d+)\.(\d+)", version)
    if not m:
        return {}
    return {"dotnet": f"{m.group(1)}.{m.group(2)}"}


def _translate_package_json(workspace_dir: str) -> dict:
    """``package.json`` ``engines.node`` → ``{"node": "<version prefix>"}`` —
    the first numeric component of the range (``^20.11`` → ``20.11``,
    ``>=20`` → ``20``, i.e. the minimum the project supports)."""
    path = os.path.join(workspace_dir, "package.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolchainError(f"package.json is unreadable/invalid: {exc}")
    engines = data.get("engines")
    spec = str(engines.get("node") or "") if isinstance(engines, dict) else ""
    m = re.search(r"(\d+(?:\.\d+)?)", spec)
    if not m:
        return {}
    return {"node": m.group(1)}


def _detect_toolchain(workspace_dir: str) -> tuple:
    """``(native, tools)`` for the workspace's declared toolchain.

    ``native=True`` → a mise-native file is present; mise reads it as-is and
    ``tools`` stays empty. Otherwise ``tools`` maps tool→version translated
    from idiomatic declarations. ``(False, {})`` → nothing declared:
    provisioning is a zero-cost no-op (the base python+node image behavior).
    """
    for name in _MISE_NATIVE_FILES:
        if os.path.exists(os.path.join(workspace_dir, name)):
            return True, {}
    tools: dict = {}
    tools.update(_translate_global_json(workspace_dir))
    tools.update(_translate_package_json(workspace_dir))
    return False, tools


def _write_translated_mise_config(tools: dict) -> str:
    """Record TRANSLATED tools in a mise config OUTSIDE the workspace and
    OUTSIDE the user's real global config.

    Not in /workspace: a generated file there would dirty the diff the review
    gate and delivery see. Not ~/.config/mise/config.toml: a host-engine run
    must never clobber the developer's own mise setup. MISE_GLOBAL_CONFIG_FILE
    points mise at the tempfile; set in ``os.environ`` so every later mise
    call — and the agent's own shells — resolve the same declaration."""
    fd, path = tempfile.mkstemp(prefix="devclaw-mise-", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("[tools]\n")
        for name in sorted(tools):
            fh.write(f'{name} = "{tools[name]}"\n')
    os.environ["MISE_GLOBAL_CONFIG_FILE"] = path
    return path


def _mise_run(args: list, workspace_dir: str, timeout: float) -> "subprocess.CompletedProcess":
    """One bounded mise invocation, cwd=workspace so mise sees the project's
    own config files. MISE_TRUSTED_CONFIG_PATHS covers the workspace (mise
    refuses untrusted config in non-interactive runs otherwise); MISE_YES
    kills any residual prompt."""
    env = dict(os.environ)
    env.setdefault("MISE_TRUSTED_CONFIG_PATHS", workspace_dir)
    env["MISE_YES"] = "1"
    return subprocess.run(
        ["mise", *args],
        cwd=workspace_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _provision_toolchain(workspace_dir: str) -> dict | None:
    """Detect and provision the project-declared toolchain.

    Returns a summary dict for the observability event (tool list + duration —
    the data that decides whether cold-start ever needs optimizing), or None
    when nothing is declared and nothing was done. Raises :class:`ToolchainError`
    on ANY failure — the caller settles the task error, never a silent skip.
    """
    native, tools = _detect_toolchain(workspace_dir)
    if not native and not tools:
        return None
    if shutil.which("mise") is None:
        # A declared toolchain with no mise on PATH is a stale sandbox image
        # or an unprepared host-engine run — the deploy-skew class
        # (lifekit-stack#93). Loud, never a silent python+node fallback.
        raise ToolchainError(
            "project declares a toolchain but `mise` is not on PATH "
            "(stale sandbox image, or DEVCLAW_ENGINE=host without mise "
            "installed on the host)"
        )
    if tools:
        _write_translated_mise_config(tools)
    started = time.time()
    try:
        install = _mise_run(["install"], workspace_dir, _TOOLCHAIN_INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ToolchainError(
            f"`mise install` exceeded {_TOOLCHAIN_INSTALL_TIMEOUT_S}s"
        )
    if install.returncode != 0:
        tail = (install.stderr or install.stdout or "").strip()[-2000:]
        raise ToolchainError(f"`mise install` failed (exit {install.returncode}): {tail}")
    # Export the provisioned environment (PATH shims, DOTNET_ROOT, …) into
    # THIS process so the agent's shells and the verify gate inherit the same
    # toolchain — "`dotnet test` must find the mise-installed SDK" is handled
    # here structurally, not per-stack.
    try:
        envp = _mise_run(["env", "--json"], workspace_dir, 60)
    except subprocess.TimeoutExpired:
        raise ToolchainError("`mise env --json` timed out")
    if envp.returncode != 0:
        tail = (envp.stderr or envp.stdout or "").strip()[-2000:]
        raise ToolchainError(f"`mise env --json` failed (exit {envp.returncode}): {tail}")
    try:
        env_map = json.loads(envp.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ToolchainError(f"`mise env --json` returned non-JSON: {exc}")
    if not isinstance(env_map, dict):
        raise ToolchainError("`mise env --json` returned a non-object")
    for key, value in env_map.items():
        # mise honors [env] tables in the (trusted) workspace's own config, so
        # this map is workspace-controlled — re-apply the OAuth-only denylist:
        # a project's .mise.toml must not reintroduce a metered API key AFTER
        # _refuse_api_key() already passed.
        if key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            continue
        if isinstance(value, str):
            os.environ[key] = value
    return {
        "native": native,
        "tools": tools,
        "seconds": round(time.time() - started, 1),
    }


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
    # The ACP agent binary itself — payload → env → claude-agent-acp default.
    try:
        acp_command = _resolve_acp_command(req)
    except ValueError as exc:
        # shlex refuses e.g. an unbalanced quote. Fail loud with the knob's
        # name instead of a bare traceback — an operator typo would otherwise
        # fail every dispatch with no hint where the bad spec lives.
        _emit_result(
            {
                "status": "error",
                "error": (
                    "invalid ACP command spec (payload acp_command / "
                    f"DEVCLAW_ACP_COMMAND): {exc}"
                ),
            }
        )
        sys.exit(2)
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

    # Provision the project-declared toolchain (ADR 0005) BEFORE the agent
    # starts, so its shells and the verify gate inherit it. Fail CLOSED with a
    # legible reason — a declared-but-unprovisionable toolchain must never
    # silently degrade to the base python+node image.
    try:
        provisioned = _provision_toolchain(workspace_dir)
    except ToolchainError as exc:
        _emit_result({"status": "error", "error": f"toolchain_provision_failed: {exc}"})
        sys.exit(2)
    except Exception as exc:  # unexpected — still fail closed, still legible
        _emit_result(
            {
                "status": "error",
                "error": (
                    f"toolchain_provision_failed: unexpected "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            }
        )
        sys.exit(2)
    if provisioned is not None:
        # Observability: tool list + duration — the measurement that decides
        # whether cold-start ever warrants baked-image optimization.
        _emit_event(
            {
                "id": None,
                "type": "ToolchainProvision",
                "source": "runner",
                "ts": time.time(),
                "payload": provisioned,
            }
        )

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

    # Last text the AGENT itself emitted (a mutable holder so the callback can
    # write it). We parse this — not the decorative captured_stdout, which also
    # echoes the prompt's literal "BLOCKED: <reason>" contract text — for the
    # engineer's honest-exit self-report after the run completes.
    last_agent_message: list[str] = [""]

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
        # Track the agent's own final message so we can honor a BLOCKED self-
        # report after the run. Best-effort — a bad event must not crash the loop.
        try:
            if (
                event.__class__.__name__ == "MessageEvent"
                and str(getattr(event, "source", "")) == "agent"
            ):
                text = _agent_message_text(payload)
                if text:
                    last_agent_message[0] = text
        except Exception:
            pass
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

    usage: dict | None = None
    try:
        with contextlib.redirect_stdout(captured_stdout):
            # acp_command is configurable (DEVCLAW_ACP_COMMAND / payload); the
            # acp_env below is still claude-shaped (CLAUDE_* vars are harmless
            # extras to a non-claude agent, but a real swap likely needs its own
            # env threaded too — see docs/reference/env-vars.md).
            agent = ACPAgent(
                acp_command=acp_command,
                acp_env={
                    "CLAUDE_CODE_EXECUTABLE": claude_exec,
                    "CLAUDE_CONFIG_DIR": claude_cfg,
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                },
                # Tier the agent's model; None → the ACP server's default.
                acp_model=acp_model,
            )
            conversation = Conversation(
                agent=agent,
                workspace=workspace_dir,
                callbacks=[on_event],
            )
            conversation.send_message(wrapped_goal)
            conversation.run()
            usage = _collect_usage(conversation)
            agent.close()
    except Exception as exc:
        # A clear usage/rate limit becomes status="rate_limited" so the host
        # pauses-and-resumes instead of retry-burning quota; anything ambiguous
        # stays status="error" (the host regex fallback classifies the text).
        err_payload = _failure_result(
            str(exc),
            trace=traceback.format_exc(),
            agent_output=captured_stdout.getvalue(),
        )
        if hook_warnings:
            err_payload["hook_warnings"] = hook_warnings
        _emit_result(err_payload)
        sys.exit(1)

    # Honest-exit: the engineer's return-contract lets it self-report
    # `BLOCKED: <reason>` when it genuinely cannot finish (a missing capability,
    # contradictory or impossible instructions). Promote that self-report to a
    # first-class terminal status so the host can surface it as a legible block
    # instead of retry-burning a doomed generic failure — a block is NOT an
    # approval, so we short-circuit BEFORE the verify gate and NEVER settle
    # "ok" (fail-closed). Parsed from the agent's OWN final message, not the
    # captured decorative stdout (which echoes the prompt's contract text).
    blocked_reason = _parse_blocked_reason(last_agent_message[0])
    repo_notes = _parse_repo_notes(last_agent_message[0])
    if blocked_reason is not None:
        blocked_payload: dict = {
            "status": "blocked",
            "reason": blocked_reason,
            "workspace_dir": workspace_dir,
            "agent_output": captured_stdout.getvalue(),
        }
        if usage:
            blocked_payload["usage"] = usage
        if repo_notes:
            blocked_payload["repo_notes"] = repo_notes
        if hook_warnings:
            blocked_payload["hook_warnings"] = hook_warnings
        _emit_result(blocked_payload)
        return

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
    if usage:
        result_payload["usage"] = usage
    if repo_notes:
        result_payload["repo_notes"] = repo_notes
    if hook_warnings:
        result_payload["hook_warnings"] = hook_warnings

    # Verify gate: the agent loop finished, but "done" means the project's own
    # test/build command passes — run it now and attach the verdict. The host
    # (TaskQueue) decides done-vs-failed from `verify.passed`; here we just run it
    # and report. Emitted as an event too so it shows in the live stream.
    if verify_cmd:
        # Point Playwright's JSON reporter at a devclaw-owned path so that IF the
        # verify gate runs browser E2E (`npx playwright test --reporter=json`),
        # the run's real counts survive to the host browser-gate. Set
        # unconditionally — harmless when the gate isn't a browser suite (nothing
        # writes the file, and the host reads its absence as fail-closed only for
        # a frontend change with a playwright config).
        report_path = os.path.join(workspace_dir, _BROWSER_REPORT_REL)
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        os.environ["PLAYWRIGHT_JSON_OUTPUT_NAME"] = report_path
        verify = _run_verify(verify_cmd, workspace_dir)
        browser_report = _read_browser_report(workspace_dir)
        if browser_report is not None:
            verify["browser_report"] = browser_report
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
