---
name: task_update
description: "Controlled-mutation rules for `~/.life-state/tasks/<id>/spec.yaml`. This is the SINGLE-WRITER contract for Task Spec status transitions — `task_dispatch`, `code-task`, and `research-task` all MUST follow these rules when changing a spec. Per §5.3 invariant 1 of ~/.life/system/autonomous-overnight-architecture.md, any skill mutating spec.yaml outside these rules is violating the contract. Read this skill before editing a spec.yaml."
---

# task_update

You are about to change a Task Spec at `~/.life-state/tasks/<id>/spec.yaml`. Stop. Read this first. The rules below exist to keep the autonomous-overnight pipeline coherent as it scales.

## What you may change

Only these four frontmatter fields. Anyone touching others is breaking the contract.

| Field | Owner | When |
|---|---|---|
| `status` | dispatcher OR runner | per the transition table below |
| `dispatch_target` | dispatcher | set ONCE when moving to `dispatched-*` |
| `dispatch_run_id` | dispatcher | set ONCE when moving to `dispatched-*` |
| `dispatched_at` | dispatcher | set ONCE when moving to `dispatched-*` |
| `completed_at` | runner | set ONCE at terminal status |
| `result_summary` | runner | set ONCE at terminal status |

## Allowed status transitions

```
ready ──► dispatched-subagent ──► done
       │                       └► blocked
       ├► dispatched-build ────► done
       │                       └► blocked
       └► dispatched-human  ────► done       (handled offline; runner is the operator)
                              └► blocked
```

**Rules:**

- `ready` is the initial state set by `task_intake`. Never write it from another skill.
- `dispatched-*` MUST be one of: `dispatched-subagent` | `dispatched-build` | `dispatched-human`.
- `done` and `blocked` are terminal — no transitions out. If you think you need `blocked → ready`, you don't; the human re-dispatch path is `rm -rf ~/.life-state/tasks/<id>/` + recreate via `task_intake`.
- Any other transition is a bug. If you find yourself wanting to write one, stop and write to `~/.life-state/queue.jsonl` instead with a `task_contract_violation_attempt` event so the curator can investigate.

## Required fields when transitioning to dispatched-*

When you set `status: dispatched-<target>`, you MUST also set in the SAME edit:
- `dispatch_target: <target>` (matches the status suffix)
- `dispatch_run_id: <id>` (subagent → OpenClaw task id; build → swarm thread id; human → null)
- `dispatched_at: <iso8601 UTC>`

## Required fields when transitioning to done|blocked

When you set `status: done` or `status: blocked`, you MUST also set:
- `completed_at: <iso8601 UTC>`
- `result_summary: <single line, ≤ 200 chars>` — high-signal, scannable. The full data lives in `result.json`.

## How to actually do the edit

1. Read `~/.life-state/tasks/<id>/spec.yaml` with Read tool.
2. Verify the current `status` allows the transition you intend (see table above).
3. If invalid: STOP. Append to `~/.life-state/queue.jsonl`:
   ```json
   {"ts":"<iso>","actor":"<your-skill-name>","event":"task_contract_violation_attempt","task_id":"<id>","from_status":"<x>","to_status":"<y>","reason":"<why this was attempted>"}
   ```
   Do NOT write the spec.
4. If valid: use Edit tool to change only the allowed fields. Preserve all other content byte-for-byte. Use single Edit calls per field if needed; don't holistic-rewrite the file.
5. Append a `spec_updated` event to `~/.life-state/tasks/<id>/run.log.jsonl` capturing the transition.

## What you may NOT change

- `task_id`, `created_at`, `created_by`, `requester_route`, `verbatim_intent`, `kind`, `acceptance_criteria`, `budget`, `target_repo`, `target_branch` — these are intake-time fields. Immutable thereafter. If the spec is "wrong" in one of these, file a new task; do not mutate.
- Anything else not listed in §"What you may change."

## Why this matters

Without single-writer discipline, you get drift: two skills race on `status`, a runner clobbers a dispatcher's `dispatch_run_id`, a sloppy edit rewrites `verbatim_intent` and the human-readable audit trail breaks. None of that is theoretical — it's how every multi-agent system in CS rots over time. The rules here are the rot prevention.

Read once, internalize, follow forever.
