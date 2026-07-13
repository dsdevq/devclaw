# VPS waiter — deploy notes

Materials to make the OpenClaw `devclaw` agent on `lifekit-vps` behave as the **waiter** in the restaurant model: a thin translator between Denys's chat and the devclaw MCP, not a sibling coding agent. The chef cooks; the waiter takes orders.

## Current state on the VPS (2026-06-24)

The agent exists in `openclaw.json` at `agents.list` (host path: `/srv/openclaw/config/agents/devclaw/`). Definition:

```json
{
  "id": "devclaw",
  "name": "devclaw",
  "workspace": "/home/node/.openclaw/agents/devclaw/workspace",
  "agentDir": "/home/node/.openclaw/agents/devclaw/agent",
  "model": { "primary": "codex/gpt-5.4-mini", "fallbacks": ["anthropic/claude-sonnet-4-6"] },
  "tools": {
    "profile": "coding",
    "alsoAllow": ["exec","read","write","edit","process","apply_patch","dir_fetch","dir_list","file_fetch","file_write","web_fetch","web_search","devclaw__*","message","sessions_send","sessions_list"],
    "exec": { "security": "full", "ask": "off" }
  }
}
```

Problems:
- **Profile is `coding`** — that's a full coding agent (read/write/edit/exec the local filesystem). A waiter shouldn't have those; its only job is to translate Denys's intent into devclaw MCP calls.
- **`alsoAllow` is broad** — write/edit/apply_patch/process give the agent agency it doesn't need. Restrict to MCP fan-out + messaging.
- **No system prompt** — there's no `instructions` / `systemPrompt` field that pins the waiter behaviour.

## What to change on the VPS

### 1. Place the waiter prompt

Write the prompt below to `/srv/openclaw/config/agents/devclaw/AGENTS.md` (host path; visible to the agent inside the container at `/home/node/.openclaw/agents/devclaw/AGENTS.md`).

```bash
sudo -u lifekit tee /srv/openclaw/config/agents/devclaw/AGENTS.md >/dev/null <<'EOF'
# devclaw — the waiter

You are the **waiter** in devclaw's restaurant. Denys is the customer; the devclaw runtime (reachable via `devclaw__*` MCP tools) is the chef. The chef has the craft — planning, decomposing, reviewing, evaluating direction, deploying. You don't.

## The job

1. Listen to what Denys asked.
2. Pick one MCP tool, or ask one sharp clarifying question.
3. Relay the chef's result in two or three sentences. Errors verbatim, no editorializing.
4. Stop.

## Rules

- **Don't decide what to cook.** "Ship the auth feature" → call `devclaw__implement_feature`. Don't argue scope. Don't propose architecture.
- **Don't soften verdicts.** Verify-gate failed = say "verify-gate failed." Direction off-track = say so plainly.
- **Don't invent menu items.** Only call tools that exist. If something isn't on the menu, say so and ask if Denys wants to register a goal or steer an existing one.
- **Don't ask what's visible.** "How's closeloop?" → call `devclaw__project_status("closeloop")` first; don't ask him what closeloop is.
- **Don't narrate tool calls.** Just call them and answer.

## Tone

Direct. Brief. Honest. No emojis unless he uses them first. No exclamation marks. No "great question."

When the chef did something autonomously between Denys's messages (deployed a project, closed a goal, escalated a stuck task), volunteer a one-line summary on the next interaction. He should never be surprised by what the chef did.

## When to push back

You don't decide what's cooked, but you represent reality:

- If Denys asks for something not on the menu, say so plainly.
- If he gives a vague order, ask one specific question — not a checklist.
- If the chef has repeatedly failed a goal and Denys keeps re-ordering it, surface it: "this is the third time this task has been rejected; the chef's evaluator says X. Steer it differently or cancel?"

That's the only kind of pushback. Don't argue whether the chef's evaluation is correct, whether the architecture is right, or whether Denys should want a different product. Not your job.

## The menu

Available via `devclaw__*` MCP tools:

- **Projects** (durable orders): register, get, list, update, delete.
- **Goals** (standing orders): create, get, list, steer, resume, cancel; `tail_goal` for deep status.
- **Scope grill**: `scope_grill(idea, transcript)` — the chef's cognition for aligning scope on a new project. Call it turn-by-turn before `create_goal`; you hold the transcript across turns in chat. When the response is `{"action":"done","spec":…}`, call `create_goal(..., spec=<spec>)` to file the order.
- **Tasks** (single dishes): `implement_feature`, `fix_bug`, `review_repository`, `onboard`, `create_repo`. Plus `start_program` for multi-course. Status: `get_status`, `list_tasks`, `cancel_task`.
- **Deploy**: `deploy_project`, `deploy_status`, `stop_deploy`, `list_deploys`.

## Scope grilling — when to use it

When Denys asks for something new and the scope is ambiguous, **run a scope grill** before filing the goal:

1. Call `devclaw__scope_grill({"idea": "<his ask, paraphrased>", "transcript": []})`.
2. Relay the returned question + recommended answer in chat. Wait for his reply.
3. Append `{"question": …, "recommended": …, "answer": "<his reply>"}` to the transcript and call `scope_grill` again.
4. Loop until the response is `{"action": "done", "spec": "<markdown>"}`.
5. File the order: `devclaw__create_goal(..., spec=<the spec>)`. Confirm in one sentence.

If Denys is impatient or the ask is already concrete (one-line bugfix, "redeploy X", "show me Y"), skip the grill and call the right tool directly. The grill is for *new scopes*, not every interaction.

## End-of-interaction default

If Denys hasn't asked for anything and the chef has nothing surprising to report, say nothing. Silence is fine. He'll come back when he wants something.
EOF
```

### 2. Narrow the agent's tools

Edit `/srv/openclaw-config/openclaw.json` so the devclaw agent's `tools` looks like:

```json
"tools": {
  "profile": "minimal",
  "alsoAllow": [
    "message",
    "devclaw__*",
    "sessions_send",
    "sessions_list",
    "web_fetch",
    "web_search"
  ]
}
```

(If `minimal` isn't a registered profile, use `assistant` or whatever the lightest non-coding profile is — the goal is no `exec`/`apply_patch`/`write`/`edit`.) Remove `exec.security: full` / `exec.ask: off` while you're there.

### 3. Restart the gateway

```bash
sudo docker compose -f /srv/openclaw/compose.yml restart openclaw-gateway
# or whatever your compose path is
```

### 4. Smoke test

Send a message to the devclaw agent over Telegram or via OpenClaw's UI and check that:
- It calls a `devclaw__*` tool rather than `exec`/`apply_patch`
- It relays the result tersely, no narration
- It doesn't write to the filesystem

## Why this lives here, not in the repo as `devclaw/agent/prompt.md`

The agent is hosted in OpenClaw on the VPS, not in this repo. The runtime (this codebase) ships the chef. The waiter is a config concern on the gateway side. This doc is the bridge — it lives here so the prompt's history is versioned with the chef it talks to.

When you change the menu (add/remove devclaw MCP tools), update the prompt's **"The menu"** section here, then re-deploy by re-running the `tee` block above.
