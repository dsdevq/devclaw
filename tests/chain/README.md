# Chain tests

End-to-end walks through devclaw's alignment-then-execution lifecycle, driven
by one realistic fixture per test. **The chain is the artifact under test, not
any single module.** What's graded: did devclaw produce the right sequence of
moves, with each link's output being a reasonable input to the next?

This is NOT a cognition-quality test on the cloud model. We don't grade
"was the verdict right?" — we grade "did our modules orchestrate the work
the way a competent engineering team would have?"

## The chain (see `~/memory/projects/devclaw/chain-map-2026-06-30.md`)

```
vague idea
  → scope_grill (turn-by-turn Q&A → spec)
  → user agrees [implicit today — gap]
  → domain research [MISSING — gap]
  → chef-side admission control [warn-only today — gap]
  → repo init / onboard / setup_cicd (MCP tools)
  → firming (lock structurally complete goal)
  → decomposition into milestones/tasks  ← LOAD-BEARING EYEBALL CHECKPOINT
  → planner picks next action per tick
  → execution with context preserved across ticks
  → leaf: "how would you implement this task" (we stop here, don't execute)
```

## v1 scope

`test_chain_crm.py` walks: grill → spec → decomposer. Stops at decomposition
because that's the load-bearing observation point: if decomposition is
coherent, the chain has earned the right to proceed. v2 will extend through
planner-leaf.

## Running

Chain tests call the live `claude` CLI and burn real quota. Opt-in only:

```bash
DEVCLAW_RUN_CHAIN_EVALS=1 .venv/bin/pytest tests/chain/ -s
```

The `-s` flag is essential — every link in the chain prints its
input/output to stdout for you to eyeball.

## Gap collection

The test walks through ALL links even when several are missing, and prints a
"GAPS SURFACED" summary at the end. The test fails when gaps exist. This is by
design: the test IS the running TODO list. As gaps fill, fail-count drops.

Today's known gaps (test will surface these explicitly):
- Domain research module (looking at real-world CRMs, distilling MVP scope)
- Chef-side admission control (chef rejects malformed goals, doesn't just warn)
- Implicit user-agreement gate (chef trusts the waiter)
- Per-project skills install (skills are host-global today)

## Adding a chain fixture

A fixture = a vague idea + synthesized user answers to whatever questions the
grill produces. Pick an idea that exercises specific links:
- "Build a CRM, React + .NET" → from-scratch, domain-research gap, MVP-scope question
- "Add OAuth to my existing FastAPI app" → existing-repo, no domain research, repo-research instead
- "Migrate this Vue 2 app to Vue 3" → existing-repo, migration-shaped, decomposition heavy
