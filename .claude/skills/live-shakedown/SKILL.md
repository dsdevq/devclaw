---
name: live-shakedown
description: Run the devclaw live shakedown — exercise the REAL pipeline (logged-in claude CLI + docker sandbox) layer by layer, L1 single task → L5 abort. Use when asked to shakedown, live-test, or validate the real pipeline, after changes to the engine/queue/goal layers, or before calling a tranche done. The pytest suite is fully stubbed, so this is the only way to validate real behavior (CI Actions is billing-locked besides). Do NOT use for ordinary test runs — that's `pytest`.
---

# Live shakedown — real pipeline, layer by layer

Exercises the actual engine seam: a logged-in `claude` driving OpenHands inside a
real docker sandbox. Work the layers strictly in order — each builds on the last,
so the first failing layer names the broken seam. Narrative background:
[`docs/live-shakedown.md`](../../../docs/live-shakedown.md).

**Cost guardrail.** Every run spends Denys's Claude Pro/Max session (OAuth, no API
key — by design). Keep goals *tiny*. Default scope is **L1–L3 + L5**; run L4 (a
durable goal build, potentially long) only when explicitly asked. If a usage limit
trips mid-run, that is *expected hardening behavior*, not a failure: the account
pauses (`paused_until`), WIP is preserved, and it auto-resumes — report it and stop.

## 0. Pre-flight gate — all must pass before anything else

```bash
docker info > /dev/null && echo docker-ok        # else: start docker / socket perms
claude --version && ls ~/.claude | head -1       # logged-in CLI, non-empty ~/.claude
python3 --version                                # ≥ 3.11
echo "${ANTHROPIC_API_KEY:-<unset, good>}"       # MUST print <unset, good>
```

A set `ANTHROPIC_API_KEY` is a hard stop (`unset` it): devclaw strips it on purpose
and the server will complain. Any other failure: fix before proceeding.

## 1. Build host + sandbox image

```bash
cd <repo root>
python -m venv .venv && source .venv/bin/activate
pip install -e .        # brings fastmcp — the drive helper needs it
docker build -t devclaw-sandbox:latest -f .sandcastle/Dockerfile .
docker image ls devclaw-sandbox:latest    # confirm
```

The image bakes a pinned `claude` CLI + ACP; the host mounts `~/.claude` read-only
into it at runtime, so auth flows without a key.

## 2. Start the server + driver

```bash
mkdir -p .shakedown
export DEVCLAW_DB=$PWD/.shakedown/devclaw.db     # keep state out of the repo
export DEVCLAW_GOALS_DIR=$PWD/.shakedown/goals   # goal-view files out of ~/memory
export DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000
devclaw-mcp 2> .shakedown/server.log &           # run in background, keep the log
```

Pass criteria: log shows `devclaw v… ready (http://0.0.0.0:8000/mcp, …, recovered=0)`
and `curl -s localhost:8000/health` returns `{"ok":true,…}`. Dashboard (human eyes):
`http://localhost:8000/dashboard`.

Driver — bundled next to this skill as `drive.py` (no `DEVCLAW_TOKEN` set → no auth).
All steps below use `$DRIVE`:

```bash
DRIVE=.claude/skills/live-shakedown/drive.py
python $DRIVE list_tasks    # [] — client works
```

## 3. The layer ladder

Run in order. **Stop at the first failing layer** — later layers can only add noise.

### L1 — single real task *(make-or-break)*

```bash
mkdir -p /tmp/sc-l1 && (cd /tmp/sc-l1 && git init -q)
python $DRIVE implement_feature \
  '{"workspace_dir":"/tmp/sc-l1","goal":"create a file hello.txt containing the text: hello from devclaw"}'
# poll: python $DRIVE get_status '{"task_id":"<id>"}'   # pending → running → done
# events: python $DRIVE get_events '{"task_id":"<id>"}'
```

**Pass:** status `done` AND `/tmp/sc-l1/hello.txt` exists.
**Proves:** host → docker → runner → OpenHands → claude → back. If L1 fails, nothing
else can work — go to Troubleshooting.

### L2 — program (planner → DAG)

```bash
mkdir -p /tmp/sc-l2 && (cd /tmp/sc-l2 && git init -q)
python $DRIVE start_program \
  '{"workspace_dir":"/tmp/sc-l2","goal":"create a Python package mathx with an add() and a mul() function, each in its own module, plus a tests/ file that imports both"}'
# watch: python $DRIVE get_program '{"program_id":"<id>"}'
```

**Pass:** task DAG fills in, tasks run in dependency order, program reaches `done`.
**Proves:** planner → DAG → dispatch.

### L3 — crash recovery (durability)

Start an L2-style program; while a task is `running`, kill the `devclaw-mcp`
process, then restart it **with the same `DEVCLAW_DB`** (same shell env, same
command). **Pass:** restart log shows `recovered=N` (N ≥ 1); orphaned `running`
tasks reset to `pending` and re-run; the program reaches `done` with **no new
submission**. Stray containers from the dead process should self-`--rm`
(`docker ps`; `docker rm -f` stragglers).

### L4 — durable goal (only when explicitly requested — long, session-hungry)

```bash
python $DRIVE scope_grill '{"idea":"a tiny CLI that converts between JSON and YAML","transcript":[]}'
# loop: append each answer to transcript, call again, until {"action":"done","spec":…}
python $DRIVE create_goal \
  '{"goal_id":"<pick>","objective":"ship the cli","workspace_dir":"/tmp/sc-l4","spec":"<the finalized spec>"}'
# watch: get_goal / tail_goal / dashboard
```

**Pass:** grill converges to a spec; goal is created and the heartbeat advances it.
Don't wait for full completion unless asked — confirm it's *moving*, report, move on.

### L5 — deliberate abort (the kill switch)

Start an L2 program, let a task reach `running`, then:

```bash
python $DRIVE cancel_task '{"task_id":"<id>"}'         # → {"cancelled":true,…}
python $DRIVE cancel_program '{"program_id":"<id>"}'   # stops scheduling + children
docker ps --filter name=devclaw-                          # sandbox gone (rm -f)
```

**Pass:** statuses go terminal `cancelled`; containers are gone. **The recovery
interplay is the point:** kill + restart the server right after a cancel — the
cancelled work must STAY cancelled (`recover()` only revives `running` rows).
`cancel_program` on an already-terminal program is a safe no-op (`{"cancelled":false}`).

## 4. Troubleshooting (first failing layer → seam)

| Symptom | Likely cause | Fix |
|---|---|---|
| task `failed`: `failed to spawn docker` | docker unreachable from host process | `docker info`; socket perms |
| task `failed`: `sandbox exited N without a result line` | runner crashed in-container | run the image by hand: `docker run --rm -v /tmp/sc-l1:/workspace -v ~/.claude:/home/agent/.claude:ro devclaw-sandbox:latest '{"kind":"implement_feature","workspace_dir":"/workspace","goal":"touch x"}'` and read stderr |
| runner: `openhands-sdk not importable` | image built wrong | rebuild image (§1) |
| agent 401 / can't auth | `~/.claude` not logged in or mounted empty | log in on host; confirm session files exist |
| server won't start: `ANTHROPIC_API_KEY` complaint | key in env | `unset ANTHROPIC_API_KEY` |
| everything stalls, owner ping about usage limit | Pro/Max cap hit → account-wide pause | expected; auto-resumes at reset — report and stop |
| containers pile up | concurrency cap too high for the box | lower `DEVCLAW_MAX_CONCURRENT` (default 4) |

Primary evidence: `.shakedown/server.log` (recovered=N, notify attempts, reaped,
spawn errors) and `get_events` per task.

## 5. Teardown + report

```bash
pkill -f devclaw-mcp                                              # the server
docker ps -a --filter name=devclaw- -q | xargs -r docker rm -f    # stragglers
rm -rf .shakedown /tmp/sc-l1 /tmp/sc-l2 /tmp/sc-l4
# keep devclaw-sandbox:latest — it's reusable
```

**Deliverable:** a per-layer verdict table — layer, pass/fail, one line of evidence
(task id + final status + artifact check). If a layer failed: the seam it names,
the exact error line from `server.log`/`get_events`, and the stopping point. Never
report a partial run as a full shakedown.
