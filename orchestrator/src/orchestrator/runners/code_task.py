"""code_task — LangGraph node that runs a `kind: code` spec via Claude Code CLI.

The agent loop happens INSIDE `claude --print`. From LangGraph's perspective this
is one opaque cognition step. From the *sandbox's* perspective everything Claude
shells out to inside that loop (`git clone`, `git checkout -b`, editor calls,
`pytest`, `git push`, `gh pr create`) is also confined to the sandbox — which is
the whole point of routing the claude invocation through `Sandbox.run` instead
of a bare `subprocess.run`.

Prior to this refactor the runner shelled out directly to `subprocess.run` (via
`_subprocess.run_claude`), giving the agent the orchestrator container's
ambient authority. Now the spec's `sandbox` field selects either:
  - `bare`       — legacy /tmp/<task_id> behaviour (default)
  - `sandcastle` — per-task Sandcastle-managed container

See `orchestrator.sandbox` for the protocol and both implementations.
"""

from __future__ import annotations

from datetime import timezone

from orchestrator.dispatch import now_utc
from orchestrator.runners._subprocess import (
    CLAUDE_BIN,
    DEFAULT_ALLOWED_TOOLS,
    SubprocessResult,
    _parse_last_json_line,
)
from orchestrator.sandbox import (
    Sandbox,
    SandcastleNotInstalledError,
    make_sandbox,
)
from orchestrator.state.models import GraphState, Result, TaskSpec


def _build_prompt(state: GraphState) -> str:
    spec = state.spec
    return f"""You are a bounded autonomous coding runner. Your task spec is below.
Read it, execute the work end-to-end, then emit a final JSON result line.

TASK SPEC:
  task_id: {spec.task_id}
  kind: {spec.kind.value}
  target_repo: {spec.target_repo}
  target_branch: {spec.target_branch}
  budget_seconds: {spec.budget.max_runtime_seconds}
  branch_name: kit/{spec.task_id}

VERBATIM INTENT:
{spec.verbatim_intent}

ACCEPTANCE CRITERIA:
{chr(10).join(f"  - {c}" for c in spec.acceptance_criteria)}

EXECUTION:
  1. mkdir /tmp/{spec.task_id} && cd /tmp/{spec.task_id}
  2. git clone https://github.com/{spec.target_repo}.git .
  3. git checkout -b kit/{spec.task_id}
  4. Implement against the acceptance criteria.
  5. git add -A && git commit -m "<concise present-tense title>" && git push -u origin kit/{spec.task_id}
  6. Idempotently ensure the `devclaw` label exists on {spec.target_repo} (safe to re-run):
       gh label create devclaw --repo {spec.target_repo} --color 1f6feb \\
         --description "Opened by the devclaw autonomous orchestrator. Branch pattern: kit/<task_id>-*. See ~/.life/projects/<project>/tasks/<id>/spec.yaml for the spec." \\
         --force 2>/dev/null || true
  7. gh pr create --base {spec.target_branch} --label devclaw --title "<title>" --body "<body>"
  8. Print a JSON object to stdout on the LAST line of your output (and nothing after it) with this exact shape:
       {{"status": "done" | "blocked", "pr_url": "<url or null>", "branch": "kit/{spec.task_id}", "files_changed": [...], "tests_passed": true|false|null, "notes": "<one line>", "blocker": "<if blocked>"}}

Stay inside the budget. If acceptance can't be met, stop and emit `status: blocked` with a blocker reason.
"""


def _run_claude_in_sandbox(
    prompt: str,
    *,
    sandbox: Sandbox,
    timeout_seconds: int,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = "acceptEdits",
) -> SubprocessResult:
    """Drive `claude --print` through `sandbox.run` and parse its JSON tail.

    Mirrors what `_subprocess.run_claude` used to do directly — same exit-code
    handling, same JSON-line parsing, same blocker reasons — but every byte
    of execution happens via the Sandbox protocol.
    """
    cmd = [
        CLAUDE_BIN,
        "--print",
        "--allowed-tools",
        allowed_tools,
        "--permission-mode",
        permission_mode,
        prompt,
    ]

    completed = sandbox.run(cmd, timeout=timeout_seconds)

    if completed.timed_out:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=-1,
            timed_out=True,
            blocker="time_budget_exceeded",
        )

    if completed.returncode != 0:
        # -1 + an empty stderr is what BareTmpdirSandbox returns when the
        # claude binary is absent (FileNotFoundError translated to non-zero).
        if completed.returncode != 0 and (
            "command not found" in completed.stderr.lower()
            or f"{CLAUDE_BIN}: not found" in completed.stderr.lower()
            or (completed.returncode == 127)
        ):
            return SubprocessResult(
                status="blocked",
                parsed_json=None,
                raw_stdout=completed.stdout,
                raw_stderr=completed.stderr or f"{CLAUDE_BIN}: command not found",
                returncode=completed.returncode,
                blocker="claude_cli_not_found",
            )
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=completed.returncode,
            blocker=f"claude_cli_exit_{completed.returncode}",
        )

    parsed = _parse_last_json_line(completed.stdout)
    if parsed is None:
        return SubprocessResult(
            status="blocked",
            parsed_json=None,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            returncode=0,
            blocker="no_parseable_result_json",
        )

    return SubprocessResult(
        status=parsed.get("status", "blocked"),
        parsed_json=parsed,
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        returncode=0,
        blocker=parsed.get("blocker") if parsed.get("status") == "blocked" else None,
    )


def _build_sandbox(spec: TaskSpec) -> Sandbox:
    """Pinch-point for sandbox construction.

    Tests monkey-patch this to inject a fake; production wires straight through
    to `make_sandbox`.
    """
    return make_sandbox(spec.task_id, kind=spec.sandbox)


def code_task_node(state: GraphState) -> dict:
    """LangGraph node — invoke Claude Code CLI for one code task."""
    spec = state.spec
    if spec.kind.value != "code":
        return {"error": f"code_task_node received non-code spec (kind={spec.kind.value})"}
    if not spec.target_repo:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="target_repo_missing",
            ),
        }

    try:
        sandbox = _build_sandbox(spec)
    except SandcastleNotInstalledError as exc:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker="sandcastle_not_available",
                notes=str(exc)[:500],
            ),
        }

    try:
        sub = _run_claude_in_sandbox(
            _build_prompt(state),
            sandbox=sandbox,
            timeout_seconds=spec.budget.max_runtime_seconds,
        )
    finally:
        sandbox.teardown()

    if not sub.ok:
        return {
            "result": Result(
                task_id=spec.task_id,
                status="blocked",
                completed_at=now_utc(),
                blocker=sub.blocker or "subprocess_failed",
                notes=f"stderr tail: {sub.raw_stderr[-500:]}" if sub.raw_stderr else None,
                runtime_seconds=spec.budget.max_runtime_seconds if sub.timed_out else None,
            ),
        }

    data = sub.parsed_json or {}
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc(),
            pr_url=data.get("pr_url"),
            branch=data.get("branch"),
            files_changed=data.get("files_changed") or [],
            tests_passed=data.get("tests_passed"),
            notes=data.get("notes"),
        ),
    }


def code_task_node_stub(state: GraphState) -> dict:
    """Deterministic stub for unit testing the graph wiring without burning tokens."""
    spec = state.spec
    return {
        "result": Result(
            task_id=spec.task_id,
            status="done",
            completed_at=now_utc().replace(tzinfo=timezone.utc),
            pr_url=f"https://example.test/{spec.target_repo}/pull/1",
            branch=f"kit/{spec.task_id}",
            files_changed=["stub.md"],
            tests_passed=True,
            notes="stub run — no real Claude invocation",
        ),
    }
