# devclaw

> **DevClaw turns approved engineering proposals into verified PRs.**

DevClaw is a pipeline, not a general-purpose autonomous coder. You write a
proposal in `~/.life/system/proposals.md` (or any project's proposals dir);
once approved, `task_intake` lowers it to a `TaskSpec` with explicit
acceptance criteria; the orchestrator dispatches a `code_task` runner that
clones the target repo into a per-task sandbox, implements the change, and
hands the result to an independent verifier. The verifier re-checks the AC
in a fresh environment, opens a PR tagged `devclaw`, and either trips the
atomic-merge gate (for changes inside the 8-rule allowlist) or routes to
human review. Every state transition is written to a per-task audit log.

It's a runtime for *pre-approved, acceptance-criteria-shaped* engineering
work — typo fixes, refactors with a stated invariant, doc reframes, the
narrower passes inside a multi-task DAG. Not a chatbot. Not a freeform
"build whatever I want" agent.

What you get on top of that core loop:

- **Design-review boundary, enforced.** `kind: code` against an unknown project, or non-atomic work without an approved proposal, is refused at intake — not by the runner, by the spec writer.
- **Bounded autonomous coding.** Clones the target repo, branches, implements, runs tests, opens a PR (tagged with the `devclaw` label so runner-opened PRs are trivially filterable), all inside the agent's budget.
- **Independent verification.** Every claimed-done task gets re-checked against its acceptance criteria in a fresh environment before the orchestrator counts it done. No self-graded passes.
- **Atomic-merge gate.** PRs that fall inside the explicit 8-rule allowlist auto-merge once green; everything else holds for human review. Contract-class and architectural changes are out of scope for auto-merge by design.
- **Quiet by default.** Chat pings only on PR-opens (atomic) or Run completions (proposal-bound). Internal failures are retried silently; only the narrow set of real blockers escalate.

## Status

**v0.1 — pre-release.** Extracted from a working personal stack (validated end-to-end on real GitHub repos 2026-05-17). Production-ready *for the operator who built it*; first-cohort external users should expect rough edges and pin a specific commit.

## What's inside

```
devclaw/
├── skills/                  # 10 OpenClaw workspace skills (the runtime contract)
│   ├── project_init/        # bootstrap a project (recon arm + Socratic arm)
│   ├── propose_change/      # RFC drafting + ship-it/edit/reject flow
│   ├── define_run/          # approved proposal → DAG (tasks + deps)
│   ├── project_curator/     # heartbeat orchestrator (autonomous Run walk)
│   ├── verify-task/         # independent QA evaluator
│   ├── code-task/           # bounded autonomous coding runner
│   ├── research-task/       # research / draft / chore runner
│   ├── task_intake/         # spec writer (the only writer of new task specs)
│   ├── task_dispatch/       # cron-fired router (15-min ticks)
│   └── task_update/         # single-writer mutation rules for specs
├── docs/
│   ├── architecture-tasks.md     # the atomic-task pipeline (Phase 5.5 in the original architecture)
│   └── architecture-curator.md   # the project / proposal / Run pipeline (Phase 5.7)
└── examples/                # sample plan.md, proposal.md, dag.yaml
```

## How it fits together

The full loop, end-to-end:

```
                              ┌──────────────────────────────────────────────┐
                              │  ~/.life/system/proposals.md (or             │
                              │  ~/.life/projects/<p>/proposals/*.md)        │
                              │  ── you write & approve ("ship it") ─────►   │
                              └──────────────────────────────────────────────┘
                                                  │
                                                  ▼
   proposal  ─►  task_intake  ─►  TaskSpec (spec.yaml, status: ready)
                                                  │
                                                  ▼
                            sweep  (cron, scans ~/.life/ for status: ready)
                                                  │
                                                  ▼
                              dispatch  (claim spec, status → dispatched-*)
                                                  │
                                                  ▼
                  code_task runner  (per-task sandbox: clone → branch → implement → tests)
                                                  │
                                                  ▼
                          verify-task  (fresh env, re-check AC against evidence)
                                                  │
                                                  ▼
                                            PR opened
                                       (label: devclaw)
                                                  │
                                  ┌───────────────┴───────────────┐
                                  ▼                               ▼
                  atomic-merge gate                       human review
              (inside 8-rule allowlist)             (architectural / contract-class
              auto-merge once green                  / outside allowlist)
                                  │                               │
                                  └───────────────┬───────────────┘
                                                  ▼
                              audit log  (per-task run.log.jsonl + result.json;
                                          state-currency audit reconciles drift)
```

For multi-task proposals, `define_run` lowers the approved proposal into a
`dag.yaml` of TaskSpecs with `depends_on` edges; `project_curator` walks the
DAG on a 30-minute heartbeat, dispatching ready nodes and verifying claimed-
done ones. Same pipeline — just fanned out.

Two execution surfaces, one runtime: the atomic path is "single PR,
~minutes"; the proposal-DAG path is "design-reviewed multi-task DAG,
hours-to-days." Both terminate the same way: verified, labelled PR, audit-
logged outcome.

## Install

### Prerequisites

- A running [OpenClaw](https://openclaw.ai/) gateway with workspace-skill support.
- Claude CLI auth (Pro subscription via `claude login`) OR a Claude / Codex API key.
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated as the principal that should open PRs.
- `git` configured with `user.name` / `user.email`.
- A directory for projects + tasks state. Default: `~/.life/projects/` and `~/.life/tasks/` (matches the [lifekit](https://github.com/dsdevq/lifekit) convention). Configurable in a future release; for v0.1, you live with the default.

### Steps

```bash
# 1. Clone next to your OpenClaw workspace
git clone https://github.com/dsdevq/devclaw.git ~/devclaw

# 2. Symlink (or rsync) the skills into your OpenClaw workspace
ln -s ~/devclaw/skills/* ~/.openclaw/workspace/skills/

# 3. Register the curator heartbeat cron (proposal-bound orchestrator)
openclaw cron add \
  --name curator_30m \
  --every 30m \
  --session isolated \
  --light-context \
  --no-deliver \
  --timeout-seconds 600 \
  --message "Run the project_curator skill. Walk all active Runs in ~/.life/projects/*/runs/*/dag.yaml, dispatch ready tasks, verify claimed-done tasks, advance completions. Honor the killswitch at ~/.life/system/cron-paused."

# 4. Register the task_dispatch cron (atomic + run-bound dispatch)
openclaw cron add \
  --name task_dispatch_15m \
  --cron "*/15 * * * *" \
  --session isolated \
  --light-context \
  --no-deliver \
  --timeout-seconds 120 \
  --message "Run the task_dispatch skill — scan ~/.life/tasks/ AND ~/.life/projects/*/runs/*/tasks/ for status: ready specs and dispatch each one."

# 5. Restart the gateway (force-recreate so env vars + skill manifest reload)
docker compose -f compose/docker-compose.yml --env-file /srv/openclaw/config/.env up -d --force-recreate openclaw-gateway
```

## Use it

In your OpenClaw-paired chat (Telegram, Discord, etc.):

```
You:  In <org>/<repo>, fix the typo "depployment" → "deployment" in README.md.

Kit:  📋 task 2026-05-17-fix-typo-x9a3 · kind=code · budget=15m
      · will run on next dispatch tick (≤15 min).

Kit (~3-10 min later):  ✅ <task_id> · PR: https://github.com/.../pull/N
```

For non-atomic work:

```
You:  propose: rework BankSync retry logic to use a circuit breaker.

Kit:  📝 Proposal drafted: BankSync circuit breaker
      ~/.life/projects/finance-sentry/proposals/2026-05-17-banksync-circuit-breaker.md
      Reply ship it, edit: <changes>, or reject.

You:  ship it

Kit:  ✅ Approved · moved → proposals-approved/
      🚀 Run defined: finance-sentry/banksync-circuit-breaker · 5 tasks
      · Curator picks up on next heartbeat (≤30 min).

Kit (~1-3h later):  🎉 Run complete: 5/5 tasks verified done.
                     PRs: [list]
                     What's next on finance-sentry?
```

For unknown projects:

```
You:  let's recon <org>/<repo>

Kit (~2-5 min later):  📚 Reconned <repo>. Wrote ~/.life/projects/<slug>/recon.md.
                        A few things I can't read out of the code:
                        1. <question>
                        2. <question>
                        ...
                        Answer when you have a minute — I'll write plan.md after.
```

See [`examples/`](./examples/) for real artifacts produced by this loop.

## Agent backend (Claude vs Codex)

Cognition is one CLI subprocess per task. Two backends live in tree:

| Backend | CLI | Auth | Status |
|---|---|---|---|
| `claude` (**default**) | `claude --print` | `claude login` → `~/.claude/` | Default until 2026-06-15 |
| `codex` | `codex exec --json` | `codex login` → `~/.codex/auth.json` | Ready, opt-in via env var |

The Codex backend exists ahead of Anthropic's [2026-06-15 Agent SDK billing
split][anthropic-billing-split]. Today, `claude --print` over a Pro/Max
subscription draws from the interactive rate-limit pool — effectively
unmetered for autonomous use. After 2026-06-15, Agent SDK / `claude -p` /
Claude Code GitHub Actions move to a separate metered monthly credit
allowance. The Codex backend uses the user's ChatGPT Pro subscription via
OAuth (no API key, no metered allowance) and preserves the marginal-$0
design intent. A follow-up PR closer to the cutover will flip the default;
this release keeps `claude` as default so the migration is reversible.

[anthropic-billing-split]: https://venturebeat.com/technology/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch

### Selecting a backend

Set `DEVCLAW_AGENT_BACKEND` in the gateway environment:

```bash
# Opt into Codex (pre-cutover testing)
DEVCLAW_AGENT_BACKEND=codex

# Pin a specific Codex model (default: gpt-5.3-codex)
DEVCLAW_CODEX_MODEL=gpt-5.3-codex
```

`select_agent_backend()` in `orchestrator.runners._subprocess` resolves the
value at runner-invocation time, so a restart of the orchestrator container
picks up the change. Unknown values log a warning and fall back to the
default.

### Codex prerequisites

The Codex CLI is a Rust binary distributed via npm. On the orchestrator host:

```bash
# 1. Install the CLI (pin a known-good version; the JSONL event schema is
#    stable but has shifted across minor versions historically).
npm install -g @openai/codex@^0.130

# 2. Sign in once with the ChatGPT account that holds the Pro subscription.
#    Writes an OAuth token to ~/.codex/auth.json; no API key needed.
codex login

# 3. Confirm the model is reachable on this account.
codex exec --json --model gpt-5.3-codex "say hello" | head -5
```

### ClawHub Codex skill bundle (optional)

The following ClawHub skills are companions to the Codex backend. They run as
user-scope OpenClaw skills (not devclaw-internal nodes) and don't need to be
wired into devclaw's pyproject.toml — install them into the operator's
OpenClaw workspace:

- `codex-sub-agents` — fan-out helper for parallel Codex runs.
- `openai-codex-operator` — interactive wrapper around `codex exec`.
- `codex-orchestration` — composition primitive for multi-step Codex flows.
- `codex-orchestrator` — DAG-shaped orchestrator over Codex sessions.
- `openai-codex-multi-oauth` — multi-account OAuth juggler when a single Pro
  subscription's 5h / weekly window is at risk.
- `codex-quota` — surfaces the current ChatGPT-side Codex quota state for
  the `status` runner output.

Install (per-user, one-shot):

```bash
clawhub install codex-sub-agents openai-codex-operator codex-orchestration \
                codex-orchestrator openai-codex-multi-oauth codex-quota
```

These are not required for `DEVCLAW_AGENT_BACKEND=codex` to work — the
adapter only depends on the `codex` binary itself. Install them if you want
the on-demand quota status + multi-account fan-out in the OpenClaw chat
surface.

## PC-side install (devclaw-mcp)

Once devclaw is running on a VPS, you can file tasks against it from your PC
without going through Telegram by registering the bundled MCP server.

It exposes two tools over stdio MCP:

- `devclaw_intake(prose, from_surface="pc-kit")` — file a task. Returns
  `{task_id, spec_path, budget_min, target_repo, state}` where `state` is
  `"new"` or `"duplicate"`. **Note**: the kwarg is `from_surface`, not
  `from` — `from` is a Python reserved word and most MCP SDKs can't expose
  it cleanly.
- `devclaw_status(task_id)` — read the current state from
  `~/.life/tasks/<id>/spec.yaml` + `result.json` on the VPS. Returns
  `{state, last_action, pr_url?, completed_at?, ...}`.

Each tool call shells out via Tailscale SSH to the VPS — no new HTTP ports,
no new auth beyond the SSH key you already use.

### Prerequisites

- Tailscale SSH between your PC and the VPS must already work:
  ```bash
  ssh lifekit@lifekit-vps 'true'   # should exit 0 without a password prompt
  ```
- `devclaw-orchestrator` must be on the VPS user's `PATH` (it is, if you
  installed devclaw on the VPS the way `orchestrator/DEPLOY.md` describes).
- A Claude Code CLI (or any other MCP client) on the PC.

### One-line install

```bash
pipx install "git+https://github.com/dsdevq/devclaw#subdirectory=orchestrator"
claude mcp add devclaw devclaw-mcp
```

That registers a `devclaw` MCP server that points at the `devclaw-mcp`
console script `pipx` just put on your `PATH`.

### Or: register manually in `~/.claude/settings.json`

```jsonc
{
  "mcpServers": {
    "devclaw": {
      "command": "python",
      "args": ["-m", "orchestrator.mcp_server"],
      "env": {
        "DEVCLAW_VPS_HOST": "lifekit-vps",
        "DEVCLAW_VPS_USER": "lifekit"
      }
    }
  }
}
```

(If you installed via `pipx`, swap `"python", ["-m", "orchestrator.mcp_server"]`
for `"devclaw-mcp"` with no args.)

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DEVCLAW_VPS_HOST` | `lifekit-vps` | Tailnet hostname of the VPS running devclaw. |
| `DEVCLAW_VPS_USER` | `lifekit` | SSH user on the VPS. |

SSH/transport failures (host unreachable, auth failure, timeout) surface as
MCP tool errors with a structured `error` field — the server stays alive
across failed calls. Restart only on config changes.

## Design principles

Lifted verbatim from the architecture docs; the prose-version is in [`docs/architecture-curator.md`](./docs/architecture-curator.md):

- **No execution without project understanding.** `kind: code` against an unknown project is refused. The only exception is the Option-B stub for atomic work on a fresh repo.
- **No non-atomic code work without an approved proposal.** Proposals are the design-review boundary.
- **No false-success.** Every claimed-done task in a Run is independently re-verified before counting.
- **Single-writer everywhere.** Each state file has exactly one writer; race conditions resolve as no-ops, not corruption.
- **No bespoke scheduler / queue / locking.** OpenClaw's cron + filesystem + skill manifest are the substrate.
- **No persona drift.** Skills are bounded; persona comes from your OpenClaw agent's identity file, not from devclaw.

## What DevClaw is NOT

- **Not a chatbot.** OpenClaw is the chatbot; devclaw is what runs *inside* it. You don't converse with devclaw, you file specs against it.
- **Not a general-purpose autonomous developer.** It does not take an unbounded "build me a SaaS" prompt and figure things out. Every run starts from a TaskSpec with explicit acceptance criteria — written by `task_intake` from an approved proposal, never from raw chat. Open-ended prompts get refused at intake.
- **Not a replacement for human review on architectural or contract-class changes.** The atomic-merge gate is governed by an explicit 8-rule allowlist. Schema migrations, public-API surface changes, auth/permission code, dependency bumps that cross majors, anything touching the merge gate itself — these never auto-merge, regardless of test status. They get a PR, a `devclaw` label, and a request for human review.
- **Not a personal memory framework.** That's [lifekit](https://github.com/dsdevq/lifekit) — devclaw composes with it but doesn't require it.
- **Not a deploy template.** That's [lifekit-stack](https://github.com/dsdevq/lifekit-stack) — same separation.
- **Not a multi-day autonomous build engine.** For tasks that need durable resume across container restart or multi-cycle critique loops, you want a separate sandboxed build engine (think OpenHands, swarm, or similar) wired behind devclaw's `BuildEngine` port. devclaw is for tasks finishable in < 4h.

## Limitations (v0.1)

This list reflects what's *still* limited as of 2026-05-21. Items already
fixed (e.g. the sweep-glob coverage gap closed by [#12](https://github.com/dsdevq/devclaw/pull/12))
have been removed.

- **Hard-coded paths.** Everything assumes `~/.life/projects/` and `~/.life/tasks/`. Configurability is v0.2.
- **Per-task sandbox isolation is in flight, not landed.** `code_task` currently runs inside the OpenClaw-gateway container with a per-task `/tmp/<task_id>/` workdir — workdir separation is real, but full process/filesystem/network isolation is not. Active TaskSpec: `2026-05-20-devclaw-sandcastle-code-task-integration-a3f1` (Sandcastle adapter behind the existing `BuildEngine` port). Until that lands, treat the runner as having gateway-container-level trust.
- **Dispatch race when cron and manual dispatch overlap.** `sweep` claims a spec by flipping its status before forking the runner, which closes the common cron-vs-cron case. The manual-vs-cron race is narrower than v0.0.x but not formally closed; killswitch the cron when you're driving dispatch by hand until the compare-and-swap path is hardened.
- **GitHub-CLI-bound.** PRs go through `gh`. GitLab / Forgejo support is contributor territory.
- **English-only triggers.** Skill descriptions trigger on English phrases. Multilingual triggers haven't been tested.

## Contributing

This is early. Real bugs > drive-by polish. If you run it for a day and something breaks, open an issue with the spec.yaml + run.log.jsonl + result.json that captured the failure. If you find a skill that's missing a contract (an unhandled status transition, a missing acceptance-criterion shape), that's the most valuable kind of report.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.

## Related

- [OpenClaw](https://openclaw.ai/) — the runtime gateway.
- [lifekit](https://github.com/dsdevq/lifekit) — file-based personal AI memory framework. devclaw composes cleanly with it.
- [lifekit-stack](https://github.com/dsdevq/lifekit-stack) — reference VPS deployment template for the whole stack.
- [Sandcastle](https://github.com/mattpocock/sandcastle) — candidate sandbox adapter for v0.2.
