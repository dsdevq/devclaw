# subclaw

Event-driven sub-agent orchestrator. Successor to [devclaw](https://github.com/dsdevq/devclaw).
Built around a queue + sub-agent tree: events land in a durable queue, the
queue-mgmt drainer claims work, and per-kind sub-agents (dev-claw,
research-claw, propose-claw, curator-claw, intake-claw, planner-claw) execute
against bounded Task Specs with hook-enforced invariants at the OpenClaw layer.

## Canonical design

The full architecture, decisions, and rationale live in
`~/.life/system/proposals.md` under the entry
`2026-05-22-event-driven-subagent-architecture`. That entry is the source of
truth — this README intentionally does not duplicate it.

## Status

Pre-alpha. Repo scaffolding only — no working implementation yet. Track
progress against the proposal.

## How this differs from devclaw

- **Adopt-over-build** — leans on OpenClaw's native sub-agent primitives
  instead of reimplementing dispatch and lifecycle from scratch.
- **Hooks enforce invariants** at the OpenClaw gateway level, so safety
  rules apply uniformly across every sub-agent kind.
- **Event-driven queue** replaces devclaw's polling-cron dispatch — work
  fires when events land, not on a timer.
- **Two fault domains** — queue-mgmt runs in its own container, separate
  from the OpenClaw gateway, so a wedged drainer never takes the gateway
  down with it.
- **Shared concepts ship via the lifekit SDK** (pip package), so subclaw
  and devclaw never duplicate type definitions during the migration.

## Sibling repo

- devclaw — https://github.com/dsdevq/devclaw
