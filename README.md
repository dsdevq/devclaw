# devclaw

> **DevClaw is the chef. The waiter — an OpenClaw agent — takes orders; devclaw cooks.**

DevClaw owns the **craft of software development as a service**: durable goals, planning, decomposition, sandbox execution (via OpenHands), pre-PR review, gate verification, durable Tailscale deploys, and grounded direction evaluation. It sits behind MCP and is called by an **OpenClaw waiter agent** that translates Denys's chat into structured tool calls — devclaw doesn't talk to the user, it cooks.

Cognition is always `claude` over a Pro/Max OAuth session — **no `ANTHROPIC_API_KEY`, no metered billing** for autonomous runs.

It is **not** a chatbot and **not** a rebuild of OpenHands. OpenHands owns the agent loop (tool use, code edits, git). DevClaw owns everything *around* it: durable goals + direction evaluation, decomposition, state, isolation, observability, delivery, and deploy.

```
Denys
  │  (chat / voice / Telegram)
  ▼
OpenClaw waiter agent          ← translates chat ↔ MCP, doesn't decide
  │
  ▼
DevClaw (the chef — this repo, FastMCP)
  ├── goal/    durable goals → heartbeat tick → plan + dispatch + evaluate
  ├── server/  FastMCP stdio + streamable-HTTP, dashboard + SSE, auth
  ├── loom/    reusable orchestration core (failure classification, test integrity)
  ├── planner.py · review_gate.py · delivery.py · deploy.py · …
  └── sandcastle_runner — `docker run --rm` per task; RO ~/.claude mount; destroyed on exit
        │
        ▼
  OpenHands (Python SDK) — agent loop, runs `claude` via ACP (Pro OAuth)
```

### Layered view — where the agent harness actually lives

> The canonical layer reference, with per-layer contracts and invariants, is **[`docs/architecture-layers.md`](./docs/architecture-layers.md)**. This README section is the high-level summary. Architectural changes are judged against the doc.

Five distinct layers below the user, and only one of them is an agent harness in the technical sense (a turn-loop hosting tool calls).

| Layer | What it is | Harness? |
|---|---|---|
| **MCP surface** (`devclaw.server`) | HTTP/stdio protocol exposing tools (`create_goal`, `get_goal`, `answer_unknowns`, …) | No — protocol |
| **GoalService + heartbeat** (`devclaw.goal`) | State machine + scheduler; owns lifecycle (`investigating → firming → executing`); ticks every ~15 min; reads on-disk state and decides the next move per goal | No — orchestrator |
| **Cognition callers** (firming, decomposer, planner, evaluator, summarizer) | One-shot `claude --print` invocations with baked prompts + goal state; return YAML the loop parses | Borderline — Claude as a reasoning API, not an interactive agent |
| **TaskQueue + sandcastle engine** (`devclaw.engine`) | Receives "do task X" → `docker run devclaw-sandbox(-dotnet):local <payload>`; streams stdout events back | No — container launcher |
| **Worker harness** (`runner.py` → `claude-agent-acp` → `claude-code` CLI + MCP servers, e.g. Playwright MCP) | The actual agent turn-loop. Tool calls (Read/Edit/Bash/browser), edits the repo, commits, exits | **Yes — the only true harness in the stack** |

DevClaw is mostly **plumbing + prompts** around that one worker harness. The reasoning is Claude's, borrowed via (a) one-shot cognition calls the loop makes for planning/firming/evaluation, and (b) the worker harness running interactively inside the sandbox. The state machine, persistence, lifecycle, and gates are the real engineering — they let one goal span days, many PRs, many evaluator passes without the owner at the desk.

### Skills + hooks — two layers, one mechanism

The worker harness reads two complementary layers of doctrine each task:

| Layer | Lives in | Owned by | Purpose |
|---|---|---|---|
| **Universal** | `/opt/devclaw/skills/` + `/opt/devclaw/hooks/` (baked into the sandbox image from `openhands-runner/skills/` and `openhands-runner/hooks/` in this repo) | DevClaw | Cross-repo doctrine — quality bar, verify-gate coverage, commit hygiene, e2e patterns. The runner prepends per-task-kind skill bundles to the goal; universal hooks run mechanical pre/post checks. |
| **Per-repo** | `<repo>/.agent/skills/` + `<repo>/.agent/hooks/` (alongside `AGENTS.md`) | The project | Project-specific notes — auth flow, migration commands, deploy steps. Agent-discovered (the universal `_common` skill tells it to `ls .agent/skills/`); per-repo hooks fire after universal ones with a `[name:repo]` tag. |

Same pattern as `AGENTS.md`: universal devclaw doctrine + per-repo project facts. The universal layer stays consistent across every cascade; the per-repo layer evolves at the project's own pace.

#### Model-agnostic invariants

The skill/hook system is deliberately neutral about which agent runs inside the sandbox. Today it's `claude-code` + `claude-agent-acp`; tomorrow it could be `codex`, `gemini-cli`, an open-source agent, anything that can read files and call tools. To keep this true, the following are invariants — do NOT add code that violates them:

- **Skills are plain markdown.** No frontmatter with model-specific fields. No `Skill(name=…)` tool invocations in the prompt — that's Claude's native skill system, not ours. Any LLM that can read a markdown file at the start of its conversation can consume our skills.
- **Hooks are bash, not settings.json entries.** Hooks live as `.sh` files in `/opt/devclaw/hooks/` or `<repo>/.agent/hooks/`. `runner.py` invokes them directly. Do not move them into a `settings.json` (Claude-Code-specific) or any other harness-native config.
- **Use MCP, not vendor-specific tool wiring.** MCP is the one cross-tool standard (Cline, Cursor, Zed, Claude Code all support it). Tools we want every agent to have go through MCP, not through Claude-Code plugins or commands.
- **Per-repo discovery is `ls .agent/skills/` + `cat`, not an agent-specific catalog API.** Any agent with file-read can find them.

The day we swap claude-code for another harness, the entire skill/hook system survives — only the `ACPAgent` call in `runner.py` changes.

## The split

| Concern | Owner |
|---|---|
| Conversation with Denys | **OpenClaw waiter agent** (system prompt + tool calls) |
| Agent loop, sandbox coding, git | **OpenHands** |
| Goal → tasks decomposition, direction eval, review gate | DevClaw |
| Task/program state | DevClaw state store (SQLite) |
| Per-task isolation | DevClaw sandcastle runner (`docker run`) |
| Durable hosting / handoff | DevClaw deploy (Tailscale, reboot-surviving) |
| Interface to the waiter | DevClaw FastMCP server |

The full rationale — including why OpenHands and sandbox isolation are **orthogonal** layers (the agent vs. the box it runs in), and why this calls `docker run` directly instead of depending on `@ai-hero/sandcastle` — lives in [`docs/architecture-v2.md`](./docs/architecture-v2.md).

## Layout

```
devclaw/
├── server/             # MCP server (FastMCP) — split by job:
│   ├── __init__.py     #   re-exports + load-order
│   ├── _state.py       #   FastMCP instance + long-lived services + env
│   ├── tools.py        #   every @mcp.tool decorator (the chef's menu)
│   ├── http.py         #   every @mcp.custom_route (dashboard, SSE, /goals/answer)
│   └── lifecycle.py    #   main() + serve loops + bearer-token auth middleware
├── goal/               # the durable goal layer (folded-in goalclaw):
│   ├── service.py      #   GoalService — the facade the server wires up
│   ├── tick.py         #   one heartbeat: check → plan → evaluate → dispatch → done-gate
│   ├── planner.py      #   next-action cognition (one claude --print per tick past the gate)
│   ├── evaluator.py    #   direction evaluation, grounded in deliveries.md
│   ├── store.py        #   on-disk mind: goal.yaml · STATUS.md · log.md · inbox.md · deliveries.md
│   ├── engine.py       #   in-process dispatch into the task queue
│   ├── research.py · merge.py · notify.py · summary.py · models.py
├── engine/             # everything that EXECUTES the work:
│   ├── __init__.py     #   the Engine protocol (one async callable)
│   ├── sandcastle.py   #   docker run --rm per task; events stream from the runner (production)
│   ├── claude_sdk.py   #   spike backend: claude --print inside the same sandbox
│   ├── host.py         #   host-side runner (no sandbox; testing only)
│   ├── stub.py         #   deterministic engine for tests + offline harness
│   ├── runner_io.py    #   shared stdout/event-stream parser
│   └── workspace.py    #   per-action pristine git checkout (devclaw owns it)
├── delivery/           # how shipped changes REACH the owner:
│   ├── __init__.py     #   engineer-authored commit → branch → push → PR
│   ├── deploy.py       #   durable Tailscale deploy hosting (reboot-surviving)
│   └── repo.py         #   gh repo creation (for create_repo)
├── quality/            # gates that judge the work past the green test gate:
│   ├── __init__.py     #   pre-PR adversarial diff review (claude)
│   ├── eval_judge.py   #   failure-mode classifier across eval runs
│   └── evals.py        #   eval scoring (pure, used by harnesses)
├── prompts/            # every system prompt as a .md file (load_prompt(slug))
├── loom/               # reusable orchestration core (engine-agnostic substrate):
│   ├── limits.py       #   usage-/rate-limit failure classifier (pure)
│   ├── test_integrity.py # gate guard: flags deleted/weakened tests in a diff (pure)
│   └── trace.py        #   run-trace recorder (cognition, ticks, dispatches, deliveries)
├── planner.py          # spec / program planner (claude --print) → task DAG
├── cognition.py        # the LLM seam — Cognition protocol + Claude/Stub impls
├── elicitation.py      # scope-grill cognition (called via the scope_grill MCP tool)
├── state_store.py      # SQLite: programs, tasks, append-only events
├── task_queue.py       # async task lifecycle, concurrency, on-settle hook → goal poke
├── project_registry.py # control plane: repos → driving goals → live status rollup
└── cli.py              # devclaw projects … (terminal face of the control plane)
openhands-runner/runner.py  # OpenHands SDK inside the sandbox; emits event/result lines
.sandcastle/Dockerfile      # per-task sandbox image
tests/                      # pytest — stubbed engine; no docker, no claude
docs/architecture-v2.md     # architectural contract — read before touching the runner/store/sandbox
```

DevClaw is all Python. The only language boundary left is the process boundary: `openhands-runner/runner.py` runs the OpenHands SDK *inside* the sandbox container, isolated from the long-running host process — it talks to the host over a line-delimited JSON protocol on stdout.

## MCP tools (the chef's menu)

| Tool | Does |
|---|---|
| `implement_feature(workspace_dir, goal, …)` | Run a single feature task |
| `fix_bug(workspace_dir, description, …)` | Run a single bug-fix task |
| `review_repository(workspace_dir, …)` | Read-only review (no writes — invariant runtime-checked) |
| `onboard(workspace_dir, …)` | Analyze a repo and write a draft `AGENTS.md` (comprehension only) |
| `setup_cicd(workspace_dir)` | Commit a self-hosted GitHub Actions workflow if none exists |
| `create_repo(name, …)` | Stand up a fresh GitHub repo for a from-scratch goal |
| `start_program(workspace_dir, goal, …)` | Decompose a large goal into a task DAG and run it |
| `get_program(program_id)` / `list_programs()` | Program status + task DAG |
| `get_status(task_id)` / `list_tasks(...)` / `get_events(...)` | Task history + replayable event feed (live SSE over HTTP) |
| `cancel_task(task_id)` / `cancel_program(program_id)` | Abort in-flight work — tears down the sandbox |

Async by default: a tool call returns a `task_id` immediately and the work runs in the background. Pass a `notify_url` to get a callback on completion/block instead of polling.

### Durable goals (the goal layer)

A `program` runs once to completion; a **goal** is a standing intent DevClaw advances across many heartbeats — and judges for *direction*, not just shipped PRs.

| Tool | Does |
|---|---|
| `create_goal(goal_id, objective, workspace_dir, done_when, backlog, …)` | Register a durable goal DevClaw drives over time |
| `get_goal(goal_id)` | Objective, phase, what's in flight, the latest direction verdict, recent log |
| `list_goals()` | All goals + phase + direction |
| `steer_goal(goal_id, message)` | Correct/redirect — recorded as steering, honored on the next tick |
| `tail_goal(goal_id, …)` | Deep read-only feed: deliveries tail (what each action actually shipped) + recent events |
| `answer_goal(goal_id, answer)` | Reply to a goal waiting on the owner (Telegram answer channel for scope questions) |
| `cancel_goal(goal_id)` | Permanently stop a goal — terminal `cancelled`, tears down any in-flight action |

**Lifecycle:** every goal moves through `investigating → firming → executing`. Investigation produces a repo discovery brief; firming sharpens the goal into a typed contract (success criteria + unknowns + conventions + blockers + stub policy); executing runs the cascade.

**Firming** (opt-in via `DEVCLAW_GOAL_FIRMING=1`) sits between investigation and execution. It reads the discovery brief plus the raw goal, surfaces the questions a human PM would ask (named, with reasoned defaults), and writes `firmed-draft.yaml` with `status: needs_owner_answers`. The owner answers via `answer_unknowns(goal_id, {…})` (typically through the OpenClaw chat). Firming re-runs against the answers and either advances to `executing` or surfaces a new round. The result is a typed contract the decomposer + evaluator + done-gate all judge against — not the loose original prose.

**Stub policy** (`Goal.stub_acceptable: list[str]`) — the done-gate refuses any clause that ships as a stub *unless* the owner has explicitly listed that clause in `stub_acceptable`. Mechanical, not vibe-based: an unauthorised stub flips its clause to unsatisfied at gate time. Firming captures owner stub authorisations from the answers.

**How a goal is driven (per heartbeat past firming):**
1. **Cheap check** (0 tokens) — poll the in-flight action via a local SQLite read.
2. **Per-delivery evidence** (0 tokens) — on a finished action, read the *full* task result (agent output + gate verdict) and append a grounded note to `deliveries.md`.
3. **Next-action plan** (1 LLM call, only past the gate) — pick the single next action from the backlog/steering and dispatch it in-process.
4. **Direction evaluation** (periodic LLM call) — every `DEVCLAW_GOAL_EVAL_EVERY` deliveries, judge whether the *delivered work* is achieving the objective; corrections are fed back as steering, a hard verdict blocks.
5. **Done-gate** — the planner's `done` is only a *proposal*; it triggers a read-only `review_repository` against the firmed `done_when` + `stub_acceptable`, and the goal closes **only if the evaluator confirms `achieved`** from that review. "Done" is gated on grounded evaluation, not on counting PRs.

The zero-token idle guard is load-bearing: an idle goal and an in-flight-still-running goal cost **0 `claude` calls** (the heartbeat is mechanism; cognition runs only when there's real work).

### The project registry (control plane)

The single source of truth for **"which repos is devclaw working on, and what's the status of each"** — one entity above the tasks/programs/goals primitives, drivable from chat, API, *and* CLI. A `Project` is a thin record (repo · workspace · status · the goal(s) driving it); it links goals **by id** and joins their live status on read, so it never caches phase and never rots.

| Tool | Does |
|---|---|
| `register_project(project_id, name, …)` | Register a repo in the portfolio (slug id; optional repo_url / workspace_dir) |
| `list_projects(status?)` | Every project + a live rollup: each linked goal's phase/direction + derived health |
| `project_status(project_id)` | Full status of one project (facts + live goal status) |
| `update_project(project_id, …)` | Update facts — pause/archive, fix repo/workspace |
| `link_goal(project_id, goal_id, unlink?)` | Attach/detach a durable goal (by id; status joined live) |
| `delete_project(project_id)` | Hard-delete a project record (goals untouched) |

Same control plane from a terminal (talks to the same stores; no server needed):

```bash
devclaw projects list                 # or: python -m devclaw.cli projects list
devclaw projects show todo-fullstack-demo
devclaw projects register todo "Todo App" --repo-url git@github.com:me/todo.git
devclaw projects link todo-fullstack-demo todo-quality-audit
```

…and a portfolio view at **`/projects`** on the HTTP dashboard.

### Durable deploy hosting

The handoff for an `achieved` goal: a running product the owner opens, not a diff to read.

| Tool | Does |
|---|---|
| `deploy_project(workspace_dir, slug)` | Durable deploy → stable Tailscale `https://<node>.<tailnet>.ts.net:<port>/` URL that survives reboots. Auto-fires when a goal reaches `achieved`. |
| `deploy_status(slug)` / `list_deploys()` | Status of one deploy (exists / running / ready + stable URL) / list them all |
| `stop_deploy(slug)` | Stop a deploy, tear down its Tailscale serve, free its VPS resources |

Tailscale wiring is best-effort + graceful-degradation: `deploy_project` attempts `tailscale serve` and, if devclaw's container can't reach tailscaled, returns the one-time serve command (which then persists across reboots). Mounting the tailscaled socket into the devclaw-mcp container makes it fully automatic with no code change.

### Reliability & quality

Built to run unattended, and to ship code worth merging:

- **Survives usage limits.** A quota / rate-limit pause is *classified*, not treated as a failure: the task is requeued and a single account-wide `paused_until` gates **both** the task queue and the goal heartbeat, which auto-resume when the cap resets — zero tokens while paused, the owner pinged once.
- **No-progress watchdog.** An executing goal that ships nothing for `DEVCLAW_GOAL_NO_PROGRESS_S` (default 6h) pings the owner once — a zero-token wall-clock check that complements the per-task timeout.
- **In-house quality gate (no third-party QC).** The engineer is briefed to *audit before extending*, and the verify gate runs a **test-integrity** check that fails the gate on deleted / skipped / weakened tests, closing the "go green by gutting the tests" path.
- **Pre-PR review gate — green means *reviewed*.** A green gate proves behaviour, not quality; it can't see a dead-code line or a frontend change it never exercises. So after the gate + test-integrity pass and **before** the PR opens, a separate `claude` pass *reads the diff* against the ticket + quality bar and returns `approve` / `request_changes`. A `request_changes` verdict feeds its located issues back through the same retry loop as a gate failure (then escalates).

## Auth (the design constraint)

DevClaw inherits a `claude` OAuth session — it never uses an API key. `ANTHROPIC_API_KEY` is **actively refused** at both the host (planner) and sandbox layers so a stray key can't silently switch autonomous runs onto metered billing. All you need is a logged-in `claude` CLI: the planner shells out to it, and the per-task sandbox bind-mounts an explicit allowlist under `~/.claude` **read-only** (the credential token + `.claude.json` identity by default; nothing else).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -r openhands-runner/requirements.txt   # only inside the sandbox image
npm install -g @agentclientprotocol/claude-agent-acp

DEVCLAW_TRANSPORT=stdio devclaw-mcp        # local dev (MCP over stdio)
# or HTTP for a long-running service:
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp
#   → MCP at /mcp, dashboards at /dashboard (programs) · /goals · /projects, SSE at /programs/:id/events
```

(`devclaw-mcp` is the console script for the server; `devclaw` is the control-plane CLI; `python -m devclaw.server` / `python -m devclaw.cli` work too.)

### Engine modes (`DEVCLAW_ENGINE`)

| Value | Engine | Isolation | Use |
|---|---|---|---|
| *(unset)* | OpenHands in a per-task **docker sandbox** | ✅ full | production |
| `host` | OpenHands **on the host** (no container) | ⚠ **none** — agent has full filesystem access | dev/CI/validation where docker is unavailable |
| `stub` | deterministic stub (no OpenHands, no claude) | n/a | harness validation (`evals/`) |

### Environment variables

Copy [`.env.example`](./.env.example) to `.env` (gitignored) and uncomment what you need — devclaw loads it on startup, and shell/systemd env always wins over it. Every var organized by purpose (transport, state, sandbox, goals, model tiering, deploy, review gate) lives in [`docs/env-vars.md`](./docs/env-vars.md). The most common ones to know:

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` or `http` |
| `DEVCLAW_PORT` | `8000` | HTTP port |
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path for state |
| `DEVCLAW_GOALS_DIR` | `~/memory/goals` | one folder per durable goal |
| `DEVCLAW_ENGINE` | *(unset)* | engine mode: unset = OpenHands sandbox, `host` / `stub` / `claude_sdk` |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | the in-sandbox coding agent's model (full id) |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | repo push + PR access for `open_pr` delivery |

For the full table (~60 vars), see [`docs/env-vars.md`](./docs/env-vars.md).

## Tests

```bash
pip install -e ".[dev]"
pytest          # planner + state store + queue/DAG + goal layer, all stubbed — no docker, no claude
```

To validate the **real** pipeline (a logged-in `claude` driving OpenHands in a docker sandbox), follow the layered runbook in [`docs/live-shakedown.md`](./docs/live-shakedown.md).

## Status

DevClaw is the live runtime. As of mid-2026 it serves as the chef behind an OpenClaw waiter agent — the spec-kit elicitation flow (`build_project` / `answer_question` / `approve_spec`) and the preview hosting module were removed (drift; vault-rejected), and the durable goal layer + Tailscale deploys carry the load.

## What this is NOT

- **Not a chatbot.** It's a backend service the OpenClaw waiter calls.
- **Not a general assistant.** It executes software-development goals, nothing else.
- **Not a rebuild of OpenHands or Claude Code.** OpenHands is the wrapper, `claude-code` + `claude-agent-acp` is the agent harness inside the sandbox; DevClaw is the orchestration above it.
- **Not novel reasoning.** The intelligence is Claude's, used twice: as a one-shot reasoning API for firming/decomposition/evaluation, and as the interactive worker harness inside the sandbox. DevClaw is the state machine + scheduler + persistence + prompts that make one goal span days.
- **Not infallible.** Autonomous means "doesn't need the next prompt," not "can't ship broken work." Today's done-gate is Claude judging Claude's output; that's structurally circular and has shipped green-tests-but-broken-UI cascades. The in-progress E2E test layer exists to break that circle with mechanical browser evidence before the evaluator weighs in.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.
