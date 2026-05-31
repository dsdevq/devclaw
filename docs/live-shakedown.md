# DevClaw — live shakedown runbook

Everything in the test suite runs against **stubs** (no `claude`, no docker). This
runbook exercises the real pipeline against the **actual engine**: a logged-in
`claude` driving OpenHands inside a real docker sandbox. Work top-to-bottom — each
layer builds on the last, so a failure tells you exactly which seam broke.

> **Cost note.** Every real run spends your Claude Pro/Max session (no API key —
> that's the design). Keep the shakedown goals *tiny*. A full `build_project` can
> run for a long time; do L1–L3 first.

---

## 0. Prerequisites

| Need | Check | Fix |
|---|---|---|
| Docker running, socket reachable | `docker info` exits 0 | start docker / add user to `docker` group |
| A logged-in `claude` CLI | `claude --version` and a non-empty `~/.claude` | `claude` then log in (Pro/Max OAuth) |
| Python ≥ 3.11 | `python3 --version` | — |
| `git` | `git --version` | — |

**No `ANTHROPIC_API_KEY` in the environment** — DevClaw refuses it on purpose. Verify:

```bash
echo "${ANTHROPIC_API_KEY:-<unset, good>}"   # must print <unset, good>
```

---

## 1. Build the host + the sandbox image

```bash
cd <repo root>
python -m venv .venv && source .venv/bin/activate
pip install -e .

# the per-task sandbox image (python3.13 + openhands-sdk + claude CLI + ACP)
docker build -t devclaw-sandbox:latest -f .sandcastle/Dockerfile .
docker image ls devclaw-sandbox:latest   # confirm it exists
```

The image bakes a pinned `claude` CLI + `claude-agent-acp`; the host mounts your
`~/.claude` read-only into it at runtime, so auth flows without an API key.

---

## 2. Start the server (HTTP, so the dashboard works)

```bash
export DEVCLAW_DB=$PWD/.shakedown/devclaw.db        # keep state out of the repo
export DEVCLAW_STATE=$PWD/.shakedown/state
export DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000
devclaw-mcp          # logs to stderr; leave running in this terminal
```

You should see: `devclaw v… ready (http://0.0.0.0:8000/mcp, db=…, recovered=0)`.

In a second terminal:

```bash
curl -s localhost:8000/health      # {"ok":true,"name":"devclaw","version":"…"}
open http://localhost:8000/dashboard   # (or just browse it) — empty for now
```

### A tiny MCP driver

Tools are MCP, not REST. Save this helper and reuse it for every step below
(no `DEVCLAW_TOKEN` set → no auth needed):

```python
# drive.py
import asyncio, json, sys
from fastmcp import Client

async def call(tool, **args):
    async with Client("http://127.0.0.1:8000/mcp") as c:
        res = await c.call_tool(tool, args)
        print(res.content[0].text)

asyncio.run(call(sys.argv[1], **json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")))
```

```bash
python drive.py list_tasks            # [] — confirms the client works
```

---

## 3. L1 — a single real task (smallest end-to-end)

Prove one OpenHands run works in a sandbox before anything fancy.

```bash
mkdir -p /tmp/sc-l1 && cd /tmp/sc-l1 && git init -q && cd -
python drive.py implement_feature \
  '{"workspace_dir":"/tmp/sc-l1","goal":"create a file hello.txt containing the text: hello from devclaw"}'
# → {"task_id":"…","status":"pending"}
```

Watch it (poll, or use the dashboard):

```bash
python drive.py get_status '{"task_id":"<the id>"}'   # pending → running → done
python drive.py get_events '{"task_id":"<the id>"}'   # the live OpenHands event stream
ls /tmp/sc-l1/hello.txt                               # the artifact, on success
```

**This is the make-or-break step.** If it reaches `done` and the file exists, the
whole engine seam (host → docker → runner → OpenHands → claude → back) works.

---

## 4. L2 — a program (planner → DAG)

```bash
mkdir -p /tmp/sc-l2 && cd /tmp/sc-l2 && git init -q && cd -
python drive.py start_program \
  '{"workspace_dir":"/tmp/sc-l2","goal":"create a Python package mathx with an add() and a mul() function, each in its own module, plus a tests/ file that imports both"}'
# → {"program_id":"…","status":"planning"}
```

```bash
python drive.py get_program '{"program_id":"<id>"}'   # watch the task DAG fill in + advance
```

Confirm tasks run in dependency order and the program reaches `done`.

---

## 5. L3 — crash recovery (the durability proof)

Start a program (L2), then **kill the server mid-run** and restart it:

```bash
# while a task is 'running':
#   Ctrl-C the devclaw-mcp terminal   (or: kill <pid>)
#   then restart it with the SAME DEVCLAW_DB:
devclaw-mcp
```

On restart the log shows `recovered=N` and the heartbeat resumes the DAG with **no
new submission** — the orphaned `running` tasks are reset to `pending` and re-run.
Confirm the program still reaches `done`:

```bash
python drive.py get_program '{"program_id":"<id>"}'
```

(In-flight sandbox containers from the dead process: `docker ps` to spot any, they
should self-`--rm`; `docker rm -f` stragglers.)

---

## 6. L4 — build a project from scratch (the grill)

```bash
mkdir -p /tmp/sc-l4
python drive.py build_project \
  '{"idea":"a tiny CLI that converts between JSON and YAML","workspace_dir":"/tmp/sc-l4"}'
# → {"project_id":"…","status":"eliciting","question":"…","recommended":"…"}
```

Answer questions until it's `ready` — each answer returns the next question:

```bash
python drive.py answer_question '{"project_id":"<id>","answer":"<your answer, or just: use your recommendation>"}'
# … repeat … → {"status":"ready","spec":"# … spec markdown …"}
```

Read the agreed spec + the interview on disk, then approve to start building:

```bash
cat $DEVCLAW_STATE/projects/<id>/spec.md
python drive.py approve_spec '{"project_id":"<id>"}'   # → {"program_id":"…"}
python drive.py get_project '{"project_id":"<id>"}'    # status: approved + the program
```

The build is now a normal program — watch it on the dashboard / `get_program`. It
may run a while; that's the point. `get_program` shows tasks grouped by milestone.

---

## 7. What to watch

- **Dashboard** `http://localhost:8000/dashboard` → click a program for the live SSE event stream.
- **`get_events`** — the raw OpenHands events per task/program (Action/Observation, etc.).
- **`$DEVCLAW_STATE/projects/<id>/`** — `idea.md`, `spec.md`, `project.json` (transcript).
- **Server stderr** — `recovered=N`, notify attempts, `reaped` logs, sandbox spawn errors.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| task → `failed`, error `failed to spawn docker` | docker not reachable from the host process | `docker info`; check socket perms |
| task → `failed`, `sandbox exited N without a result line` | runner crashed inside the container | run the image by hand: `docker run --rm -v /tmp/sc-l1:/workspace -v ~/.claude:/home/agent/.claude:ro devclaw-sandbox:latest '{"kind":"implement_feature","workspace_dir":"/workspace","goal":"touch x"}'` and read stderr |
| runner error `openhands-sdk not importable` | image built wrong | rebuild the sandbox image (§1) |
| agent can't auth / 401 from claude | `~/.claude` not logged in, or mounted empty | log in on the host; confirm `~/.claude` has session files |
| server won't start, `ANTHROPIC_API_KEY` complaints | a key is set in the env | `unset ANTHROPIC_API_KEY` |
| build never starts after `approve_spec` | spec didn't plan (claude planner failed) | check stderr; `get_project` shows status; re-`approve_spec` is idempotent |
| many containers pile up | global cap too high for the box | lower `DEVCLAW_MAX_CONCURRENT` |

---

## 9. Teardown

```bash
# stop the server (Ctrl-C), then:
docker ps -a --filter name=devclaw- -q | xargs -r docker rm -f   # any stragglers
rm -rf .shakedown /tmp/sc-l1 /tmp/sc-l2 /tmp/sc-l4
# the sandbox image is reusable; remove only if you want: docker rmi devclaw-sandbox:latest
```

---

## Note on CI

CI Lint is red on every PR because the GitHub Actions account is billing-locked —
no job starts, regardless of code. That's infrastructure, not a code failure; this
runbook is how you actually validate behavior until Actions is restored.
