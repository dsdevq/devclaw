# docs/ — index & currency map

Every doc in `docs/`, its one-line purpose, and a **currency tag** so a new reader
knows what to trust. Tags:

- **CURRENT** — spot-checked against code; load-bearing claims hold.
- **HISTORICAL** — a decision record / point-in-time snapshot, true as history, not a live spec.
- **STALE — see note** — contains at least one claim the code contradicts; note says what.

Currency is verified by grepping each doc's load-bearing claims against the code, not
by trusting the doc's own "Status:" line. When you change behavior that a doc
describes, fix the doc **and** update its tag here in the same PR.

| Doc | Purpose | Currency |
|---|---|---|
| [`architecture-v2.md`](./architecture-v2.md) | The founding decision record: adopt OpenHands as the execution engine; devclaw is thin orchestration. Now also carries the OpenHands-vs-isolation orthogonality rationale + why devclaw calls `docker run` itself. | **CURRENT** — *fixed in this PR.* Previously STALE: it described a never-built REST-polled OpenHands service (poller, `OPENHANDS_URL`, `openhands:3000` sibling container). Corrected to the ephemeral `docker run --rm` model the code actually uses. |
| [`architecture-layers.md`](./architecture-layers.md) | The locked 5-layer model + per-layer contracts, invariants, testability/replaceability. | **CURRENT** — *fixed in this PR.* Dead test-file citations (`test_mcp_tools*.py`, `test_sandcastle.py`, `test_task_queue.py`, `test_phase_*.py` — none exist) replaced with the real files that cover each layer. |
| [`task-execution-flow.md`](./task-execution-flow.md) | Temporal trace of ONE task, every hop (node 1 waiter → node 2 devclaw-mcp → node 3 ephemeral sandbox), with a "fails if" rail. | **CURRENT** — matches `engine/sandcastle.py`: per-task `docker run --rm`, RO `~/.claude` mount, stdout `event:`/`result:` protocol. |
| [`delivery-flows.md`](./delivery-flows.md) | How dispatches become PRs: the 3 delivery shapes (backlog / checklist / program), the dispatch cap, the Shape-3 reconcile step. | **CURRENT** — reflects the #172/#173 refund logic and the 2026-07-09 reconcile step; matches `goal/merge.py`. |
| [`env-vars.md`](./env-vars.md) | Single source of truth for every env var the runtime reads, grouped by purpose. | **CURRENT** — includes the per-project overrides (merge_strategy / autodeploy / review_gate / verify_done, #164) and the cognition/model-tiering seam. |
| [`engine-decision.md`](./engine-decision.md) | Decision log + procedure for the OpenHands-vs-`claude_sdk` engine choice; the switch is still a draft pending a live comparison. | **CURRENT** — `claude_sdk` remains an opt-in spike (`DEVCLAW_ENGINE=claude_sdk`); OpenHands is still the unset-engine default. |
| [`live-shakedown.md`](./live-shakedown.md) | Runbook for exercising the real pipeline (logged-in `claude` + docker) layer by layer (L1 single task → L5 abort). | **STALE — see note.** L1–L5 runbook is accurate. But §7 still lists `$DEVCLAW_STATE/projects/<id>/{idea,spec,project.json}` and the troubleshooting table still references `approve_spec` / `build_project` / `get_project` — the removed spec-kit elicitation flow. Not fixed here (flagged for triage). |
| [`vps-waiter-deploy.md`](./vps-waiter-deploy.md) | The OpenClaw waiter-agent prompt + how to narrow its tools/config on the VPS. Bridge doc: the waiter lives on the gateway, not in this repo. | **CURRENT** (operational, VPS-side; a 2026-06-24 config snapshot). The waiter prompt's "menu" tracks the live MCP tool set (scope_grill, create_goal, implement_feature, deploy_project, …). |
| [`INDEX.md`](./INDEX.md) | This file. | **CURRENT** |

## Note on `docs/architecture-v2.md`'s own "Status: Live"

Docs carry their own status headers; those are self-reported and can rot. This index
is the external check. Where a doc's header and this table disagree, trust the table —
its tags come from grepping code, not from the doc.
</content>
