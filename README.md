# devclaw

> Turn an [OpenClaw](https://openclaw.ai/) gateway + a Claude or Codex CLI auth into an autonomous-development collaborator. Send a chat message; get a merged PR.

`devclaw` is a bundle of OpenClaw workspace skills that gives your assistant a real development workflow:

- **Project understanding** — recons existing repos, runs Socratic planning for new ones, refuses to act on code it doesn't understand.
- **Design review boundary** — non-atomic work goes through an RFC-style proposal you approve before anything autonomous runs.
- **Bounded autonomous coding** — clones target repo, branches, implements, runs tests, opens a PR, all inside the agent's budget.
- **Independent verification** — every claimed-done task in a Run gets re-checked against its evidence in a fresh environment before the orchestrator counts it done.
- **Quiet by default** — chat pings only on PR-opens (atomic) or Run completions (proposal-bound). Internal failures are retried silently; only the narrow set of real blockers escalate.

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

```
You (chat)  ─►  task_intake  ─►  spec.yaml  ─►  task_dispatch  ─►  code-task / research-task  ─►  PR / artifact
                   │
                   └─ refuses unknown projects, refuses non-atomic without an approved proposal

You (chat)  ─►  propose_change  ─►  proposal RFC (you read)  ─►  ship it  ─►  define_run  ─►  dag.yaml
                                                                                                  │
                                                          curator_30m cron  ─►  project_curator ──┤
                                                                                   ├─ dispatch ready nodes
                                                                                   ├─ verify-task each claimed_done
                                                                                   ├─ internal retry on failure
                                                                                   └─ announce on Run complete
```

The atomic path (top) is "single PR, ~minutes." The proposal path (bottom) is "design-reviewed multi-task DAG, hours-to-days." Same runtime, two execution surfaces.

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

## What devclaw is NOT

- **Not a chatbot.** OpenClaw is the chatbot; devclaw is what runs *inside* it.
- **Not a personal memory framework.** That's [lifekit](https://github.com/dsdevq/lifekit) — devclaw composes with it but doesn't require it.
- **Not a deploy template.** That's [lifekit-stack](https://github.com/dsdevq/lifekit-stack) — same separation.
- **Not a multi-day autonomous build engine.** For tasks that need durable resume across container restart or multi-cycle critique loops, you want a separate sandboxed build engine (think OpenHands, swarm, or similar) wired behind devclaw's `BuildEngine` port. devclaw is for tasks finishable in < 4h.

## Limitations (v0.1)

- **Hard-coded paths.** Everything assumes `~/.life/projects/` and `~/.life/tasks/`. Configurability is v0.2.
- **No per-task sandbox isolation yet.** `code-task` runs inside the OpenClaw-gateway container itself with a per-task `/tmp/<task_id>/` workdir. Workdir separation is real; full sandbox isolation (via [sandcastle](https://github.com/mattpocock/sandcastle) or similar) is on the v0.2 roadmap.
- **Race condition in `task_dispatch`** when the cron and a manual dispatch fire on the same `status: ready` spec. Logged but not yet fixed. Killswitch the cron when you're driving dispatch manually until v0.2 lands the compare-and-swap fix.
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
