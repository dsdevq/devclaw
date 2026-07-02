# Task execution flow — every hop, top to bottom

A step-by-step trace of what happens when devclaw runs ONE task (an
`implement_feature`, `fix_bug`, `review_repository`, etc.). Sibling
reference to [`architecture-v2.md`](./architecture-v2.md): that file
describes the *structural* choice (why three layers); this one walks the
*temporal* sequence (what each layer does, in order, for one task).

Use this when:
- a task fails and the symptom is ambiguous — find which step's column in
  the "fails if" rail matches the symptom;
- onboarding a reader who needs to know *what code runs when*;
- proposing a change — pin the step you're touching so the blast radius is
  scoped to one row.

The three nodes (Node 1 = openclaw waiter / TS, Node 2 = devclaw-mcp /
Python, Node 3 = ephemeral sandbox / per task) are defined in
[`architecture-v2.md`](./architecture-v2.md) and in
`~/memory/projects/devclaw/architecture.md`. This doc assumes you've read
one of those.

## The full sequence

```
TIME │  ACTOR / NODE                      │  WHAT HAPPENS                                   │  FAILS IF
─────┼────────────────────────────────────┼─────────────────────────────────────────────────┼──────────────────────
  ●  │  Denys (chat / voice / Telegram)   │  "build the read-only finance-sentry MCP"       │
     │                                    │                                                 │
  │  │  ┌─────────────────────────────┐                                                     │
  ▼  │  │  NODE 1 — openclaw waiter   │                                                     │
  ●  │  │  (Node.js, MCP CLIENT)      │                                                     │
     │  │                             │                                                     │
     │  │  Translates chat into:      │                                                     │
     │  │    create_goal(             │                                                     │
     │  │      objective="...",       │                                                     │
     │  │      verify_cmd="dotnet     │                                                     │
     │  │        test ...",           │                                                     │
     │  │      workspace_dir="..." )  │                                                     │
     │  │                             │                                                     │
     │  │  Sends JSON-RPC over MCP    │  ──────► HTTP POST /mcp                             │
     │  └─────────────────────────────┘                                                     │
     │                                                                                      │
     │  ┌─────────────────────────────────────────────────────────────┐                     │
     │  │  NODE 2 — devclaw-mcp  (Python, MCP SERVER + orchestrator)  │                     │
     │  │  long-lived; runs as `node`; needs docker.sock + GID 990    │  ◄── perm-denied if .env
     │  │                                                             │      LIFEKIT_DOCKER_GID
     │  │  Step A — @mcp.tool create_goal runs:                       │      missing (2026-06-25 bug)
     │  │     • write /var/lib/devclaw/goals/<id>/{goal.yaml,         │
     │  │       STATUS.md, log.md, deliveries.md}                     │
     │  │     • lifecycle="investigating", phase="idle"               │
     │  │     • return {goal_id, ...} to the waiter                   │
     │  │                                                             │
     │  │  Step B — heartbeat (15-min loop in goal/service.py):       │
     │  │     • read STATUS.md + inbox.md (cheap, 0 tokens)           │
     │  │     • if phase==idle and no in_flight:                      │
     │  │         planner.py invokes `claude --print` ◄────┐          │
     │  │           - reads inbox steering + log tail      │          │
     │  │           - emits a JSON next-action plan        │          │
     │  │     • dispatches the next implement_feature      │          │
     │  │                                                  │ Pro/Max OAuth (~/.claude on host),
     │  │  Step C — engine/sandcastle.run_sandcastle():    │ no API key
     │  │     • _validate_workspace(workspace_dir)          │                                │ PR #117: empty dir → fail-fast
     │  │     • host_bind = _translate_workspace_path(     │                                 │
     │  │         workspace_dir )                          │                                 │
     │  │     • docker run --rm --name devclaw-XXXX        │ ──┐                             │
     │  │         -v <host_bind>:/workspace                │   │                             │ docker run fails here
     │  │         -v ~/.claude/.credentials.json:RO        │   │                             │ if Node 2 lacks GID 990
     │  │         -v ~/.claude/.claude.json:RO             │   │
     │  │         --tmpfs /home/agent/.claude/session-env  │   │
     │  │         --network host                           │   │
     │  │         devclaw-sandbox-dotnet:local             │   │
     │  │         '<JSON payload>'                         │   │
     │  └──────────────────────────────────────────────────┘   │                             │
     │                                                         │                             │
     │  ┌──────────────────────────────────────────────────┐   │                             │
     │  │  NODE 3 — ephemeral sandbox (per task)           │   ▼                             │
     │  │  devclaw-XXXXXXXX  — runs as `agent`             │                                 │
     │  │  ENTRYPOINT: python /opt/devclaw/runner.py       │                                 │
     │  │  NO docker.sock. NO host personal dirs.          │                                 │
     │  │                                                  │                                 │
     │  │  Step D — runner.py:                             │                                 │
     │  │     • read JSON payload from argv                │                                 │
     │  │     • build wrapped_goal (system prompt +        │                                 │
     │  │       quality bar + the task body)               │                                 │
     │  │     • from openhands.sdk import Conversation,    │                                 │
     │  │           ACPAgent                               │                                 │
     │  │     • agent = ACPAgent(                          │                                 │
     │  │           acp_command=["claude-agent-acp"],      │                                 │
     │  │           acp_model="claude-sonnet-4-6")         │                                 │
     │  │     • conv = Conversation(agent, workspace=      │                                 │
     │  │           "/workspace", callbacks=[on_event])    │                                 │
     │  │     • conv.send_message(wrapped_goal)            │                                 │
     │  │     • conv.run()    ──────► spawns subprocess:  │                                  │
     │  │                                                  │                                 │
     │  │     ┌──────────────────────────────────────┐     │                                 │
     │  │     │  claude-agent-acp  (a binary)        │     │                                 │
     │  │     │  └─ claude  (Claude Code CLI)        │     │                                 │
     │  │     │     │                                │     │                                 │
     │  │     │     │ Uses ITS OWN tools — Bash,     │     │                                 │
     │  │     │     │ Read, Edit, Write, Grep —      │     │                                 │
     │  │     │     │ NOT OpenHands' (those are      │     │                                 │
     │  │     │     │ NotImplementedError on the     │     │                                 │
     │  │     │     │ ACP path).                     │     │                                 │
     │  │     │     │                                │     │                                 │
     │  │     │     │ Reads /workspace/CLAUDE.md +   │     │                                 │
     │  │     │     │ AGENTS.md, plans, edits files  │     │                                 │
     │  │     │     │ via Bash + Write, runs         │     │                                 │
     │  │     │     │ commands inside /workspace.    │     │                                 │
     │  │     │     │                                │     │                                 │
     │  │     │     │ Streams events back to the     │     │                                 │
     │  │     │     │ ACPAgent in the runner via     │     │                                 │
     │  │     │     │ JSON-RPC over stdio.           │     │                                 │
     │  │     │     │                                │     │                                 │
     │  │     │     │ Finishes turn → FinishAction.  │     │                                 │
     │  │     └──────────────────────────────────────┘     │                                 │
     │  │                                                  │                                 │
     │  │  Step E — every event from claude:               │                                 │
     │  │     on_event callback writes one line:           │                                 │
     │  │       event: {"id":..., "type":..., ...}         │  ──► stdout (devclaw-mcp reads) │
     │  │                                                  │                                 │
     │  │  Step F — when conversation.run() returns:       │                                 │
     │  │     verify = subprocess.run(verify_cmd,          │                                 │
     │  │         cwd="/workspace", timeout=…)             │  ◄── must succeed (exit 0)      │ PR #117 sibling needed:
     │  │     # this is `dotnet test ...` for finance-sentry                                 │ pre-check `command -v dotnet`
     │  │                                                  │                                 │ in the sandbox before
     │  │  Step G — emit terminal line:                    │                                 │ spawning, fail-fast if missing
     │  │     result: {"status":"ok","verify":{...},       │                                 │ (the dotnet-not-found bug)
     │  │              "message":"..."}                    │  ──► stdout                     │
     │  └──────────────────────────────────────────────────┘                                 │
     │                  │                                                                    │
     │       container  │  exits → docker --rm vaporizes it.  Tmpfs gone, scratch gone.      │
     │                  ▼                                                                    │
     │  ┌─────────────────────────────────────────────────────────────┐                      │
     │  │  Back in NODE 2 — devclaw-mcp (consume_runner_output):      │                      │
     │  │                                                             │                      │
     │  │  Step H — parse the stream:                                 │                      │
     │  │     • event: lines → on_event callback (logging, dashboard) │                      │
     │  │     • final result: line → EngineResult                     │                      │
     │  │     • if no result line → "sandbox exited 1 without a       │                      │
     │  │       result line" (the misleading error string)            │                      │
     │  │                                                             │                      │
     │  │  Step I — review gate (quality/review_diff):                │                      │
     │  │     • git diff main..HEAD of the workspace                  │                      │
     │  │     • feed diff to `claude --print` for adversarial check   │                      │
     │  │     • test-integrity guard: were tests deleted/weakened?    │                      │
     │  │     • either: ok / needs revision (kicked back to engineer) │                      │
     │  │                                                             │                      │
     │  │  Step J — delivery (delivery.deliver_change):               │                      │
     │  │     • git push to branch goal/<slug>                        │                      │
     │  │     • gh pr create  (conventional commit + diffstat body)   │                      │
     │  │     • record PR URL                                         │                      │
     │  │                                                             │                      │
     │  │  Step K — append to deliveries.md (grounded evidence        │                      │
     │  │     for the direction evaluator).                           │                      │
     │  │     Save STATUS, set in_flight=null, last_progress_at=now.  │                      │
     │  │                                                             │                      │
     │  │  Step L — notify (notify_url POST to notify-relay:8090):    │                      │
     │  │     → Telegram message to Denys with PR link + gate verdict │                      │
     │  └────────────────────────────┬────────────────────────────────┘                      │
     │                               │                                                       │
     │                               ▼                                                       │
     │                       Denys's phone buzzes                                             │
     │                                                                                       │
     ▼  back to the heartbeat — next tick decides: more work, direction-eval, or done-gate.  │
```

## How the 2026-06-25 cascade maps onto these steps

| Failure | Step | Symptom seen by the planner | Real fix |
|---|---|---|---|
| Empty workspace bind | Step C — `_translate_workspace_path` passed an out-of-prefix path → host bind was empty | Sandbox starts, claude finds empty repo, hits wall-clock | PR #117 — `_validate_workspace()` in Step C |
| Wrong sandbox image | Step C — `DEVCLAW_SANDBOX_IMAGE=devclaw-sandbox:local` (no dotnet) | Sandbox runs, claude writes scaffold OK, **Step F** `dotnet test` exits 127 | `.env` + `compose/docker-compose.override.yml` to pin `devclaw-sandbox-dotnet:local` |
| Docker GID drift | **Before** Step C — the very `docker run` in Step C never succeeds | Permission denied at `/var/run/docker.sock`, "sandbox exited 1 / no result line" — looks identical to Step H's "no terminal result" | `compose/.env` with `LIFEKIT_DOCKER_GID=990`; also: a fail-fast precheck in run_sandcastle that exec's `docker version` once on startup |

All three were silent-timeout failures because the engine output in Step H
couldn't distinguish *"the docker run from Step C never happened"* from
*"the sandbox started but exited 1"*.

## Open improvements (queue, not yet built)

1. **Failure-mode disambiguation in Step H.** Distinct error strings for:
   "the `docker run` in Step C never returned a container ID" vs "the
   sandbox started, ran, and exited 1" vs "the sandbox produced no
   `result:` line." All three look identical today; the planner has to
   guess and usually picks the wrong fix.
2. **Toolchain precheck (Step C, sibling of PR #117).** Before `docker
   run`, exec `command -v <first-token-of-verify_cmd>` inside the sandbox
   image and refuse to spawn if missing. Same shape as `_validate_workspace`.
3. **Healthcheck verifies docker access (devclaw-mcp service).** Today's
   healthcheck is `curl /health`; doesn't catch the GID drift. Add a
   `docker version` to the test so a broken-but-running orchestrator flips
   unhealthy in 30s instead of being discovered by the first task.
4. **Sandbox image selection by detected stack.** Historically we planned to
   reuse a `_detect_stack()` helper to pick the sandbox image automatically per
   task instead of one global `DEVCLAW_SANDBOX_IMAGE` env. That helper lived in
   `devclaw/server/tools.py` as part of the removed `setup_cicd` scaffolder and
   was deleted along with it (its 5-stack template list was silently wrong for
   fullstack repos). Any future revival needs to be per-task stack judgment by
   an engineer task, not a hardcoded dict — same reason as the C5 shift.
5. **AGENTS.md posture block for sandbox.** Generated `AGENTS.md` should
   prepend "you're in a docker-less sandbox; ignore stack-bring-up
   instructions from `CLAUDE.md`; use the unit-only verify path." Today
   the agent has to figure it out from instruction overrides.
