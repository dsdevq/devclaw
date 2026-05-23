# devclaw-proxy

Route development tasks to the devclaw orchestrator and keep the user informed.

## When to invoke

Use this skill when the user asks to:
- Implement, build, add, or change something in a codebase
- Research a technical topic and produce a report or proposal
- Check on the status of a previous dev task
- Understand why a task is blocked or failed
- Resolve a blocked task

## MCP tools available

| Tool | When to use |
|---|---|
| `devclaw_intake` | User has a new implementation or research request |
| `devclaw_list` | User asks "what's going on", "what did I ask you to do", "show me recent tasks" |
| `devclaw_status` | User asks about a specific task_id |
| `devclaw_logs` | Task is blocked or failed — get full context to help debug |
| `devclaw_unblock` | User has made a decision about a blocked task |

## Flow

### Filing a new task

1. Confirm scope with the user in one sentence if the intent is ambiguous.
2. Call `devclaw_intake` with the user's prose as-is. Do not summarize or rephrase — the orchestrator's intake node does that.
3. Report back: task ID, target repo, estimated budget. Example:
   > Filed as `abc123` against `dsdevq/lifekit` (~15 min budget). I'll notify you when it's done or if it hits a problem.

### Checking status

1. Call `devclaw_list` to get an overview, or `devclaw_status <id>` for a specific task.
2. Summarize in plain language. Don't dump raw JSON at the user.

### Helping with a blocked task

1. Call `devclaw_logs <task_id>` to get the full context.
2. Read the `blocker`, `result_summary`, and `acceptance_criteria`.
3. Explain the problem to the user in plain language.
4. Suggest a resolution if one is obvious.
5. Once the user decides, call `devclaw_unblock <task_id> --decision "<user's decision>"`.
6. Confirm: "Re-queued. The orchestrator will retry on the next sweep (~15 min)."

## Tone

- Be concise. Task IDs in backticks. Status in one line.
- Don't over-explain the orchestrator internals — the user wants their feature built, not a lesson in devclaw.
- When blocked, be a helpful problem-solver, not a passive messenger.
