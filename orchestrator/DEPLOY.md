# Deploying `devclaw-orchestrator` to the VPS

Operational runbook for cutting over from the markdown skill execution model to the Python orchestrator. The orchestrator's code surface is complete; this doc covers the deploy + cutover steps that need to run on a live host.

**Read this before flipping any crons.** The markdown crons and the Python crons race over the same `~/.life/tasks/*/spec.yaml` files. Run them concurrently and you get double-dispatches.

## Pre-flight

On the VPS (assumes `denys@<vps>` SSH access via Tailscale):

- [ ] `claude --version` runs cleanly and reports authenticated session at `~/.claude/`
- [ ] `gh auth status` is OK (push access to target repos)
- [ ] `~/.life/` is reachable and writable (SSHFS mount or VPS-canonical)
- [ ] Python 3.11+ available: `python3 --version`
- [ ] `uv` installed: `which uv` (otherwise: `curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Step 1 — Install the orchestrator on the VPS

```bash
ssh denys@<vps>

# Clone or update devclaw alongside the existing markdown skills location
sudo -u lifekit git -C /srv/openclaw/workspace/devclaw pull

# Install the orchestrator package — system venv at /opt/devclaw-orchestrator/.venv
sudo mkdir -p /opt/devclaw-orchestrator
sudo chown lifekit:lifekit /opt/devclaw-orchestrator
cd /opt/devclaw-orchestrator
sudo -u lifekit uv venv
sudo -u lifekit uv pip install -e /srv/openclaw/workspace/devclaw/orchestrator

# Verify
sudo -u lifekit /opt/devclaw-orchestrator/.venv/bin/devclaw-orchestrator --help
```

Expected: usage output with the five subcommands (`dispatch`, `intake`, `sweep`, `supervise`, `supervise-all`).

## Step 2 — Smoke-test against the live `~/.life/`

```bash
sudo -u lifekit /opt/devclaw-orchestrator/.venv/bin/devclaw-orchestrator sweep --life /srv/life --quiet
sudo -u lifekit /opt/devclaw-orchestrator/.venv/bin/devclaw-orchestrator supervise-all --life /srv/life
```

Both should exit 0. `sweep` reports `scanned=N dispatched=0 reaped=0 ghosted=0 errors=0` if everything is in a clean state. `supervise-all` reports per-Run summaries or `no active runs`.

## Step 3 — Pause the markdown crons (before adding Python crons!)

Avoid double-dispatch. Pause via killswitch first, swap crons, then unpause.

```bash
ssh denys@<vps>
touch /srv/life/system/cron-paused
```

Both the markdown and Python entry points honor this killswitch — they short-circuit when the file exists.

## Step 4 — Add Python crons via OpenClaw

```bash
sudo -u lifekit openclaw cron add \
  --name sweep_15m \
  --schedule '*/15 * * * *' \
  --timezone Europe/Dublin \
  --command '/opt/devclaw-orchestrator/.venv/bin/devclaw-orchestrator sweep --life /srv/life --quiet'

sudo -u lifekit openclaw cron add \
  --name supervise_30m \
  --schedule '*/30 * * * *' \
  --timezone Europe/Dublin \
  --command '/opt/devclaw-orchestrator/.venv/bin/devclaw-orchestrator supervise-all --life /srv/life'
```

Verify:

```bash
sudo -u lifekit openclaw cron list
```

Should show both new entries plus the existing markdown ones (still paused via killswitch).

## Step 5 — Remove (or disable) the markdown crons

Once you're confident in the Python crons:

```bash
sudo -u lifekit openclaw cron remove task_dispatch_15m
sudo -u lifekit openclaw cron remove curator_30m
```

Or pause indefinitely if you want the option to roll back:

```bash
sudo -u lifekit openclaw cron disable task_dispatch_15m
sudo -u lifekit openclaw cron disable curator_30m
```

## Step 6 — Unpause and watch

```bash
rm /srv/life/system/cron-paused
```

Next cron tick (within 15 min) fires the new `sweep` entry. Watch:

```bash
journalctl -u openclaw-gateway -f | grep -E 'sweep|supervise'
```

Healthy first ticks show `sweep scanned=N dispatched=0 reaped=0 ghosted=0`. Then `supervise-all: no active runs` or per-Run summaries.

## Step 7 — Optional: Postgres checkpointer

The default SQLite checkpointer at `~/.life/orchestrator.sqlite` is fine for single-VPS dev. For production durability across VPS reboots — or eventually cross-machine — install the Postgres extra and switch:

```bash
sudo -u lifekit uv pip install -e '/srv/openclaw/workspace/devclaw/orchestrator[postgres]'
```

Then in your cron commands pass a `--db postgres://user:pass@host:5432/orchestrator` (CLI flag added in a future PR — today the dispatch CLI only takes `--db <sqlite-path>`).

For finance-sentry's Postgres, create a dedicated DB:

```bash
sudo -u postgres createdb orchestrator
sudo -u postgres psql -c "CREATE USER orchestrator WITH PASSWORD '<gen-strong-pw>';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE orchestrator TO orchestrator;"
```

LangGraph's `PostgresSaver.setup()` (called by `postgres_checkpointer()` on first connect) creates its own tables idempotently.

## Rollback

If anything goes sideways:

```bash
ssh denys@<vps>
touch /srv/life/system/cron-paused          # kill new crons
sudo -u lifekit openclaw cron enable task_dispatch_15m
sudo -u lifekit openclaw cron enable curator_30m
rm /srv/life/system/cron-paused             # bring back the markdown crons
```

The Python and markdown systems coexist on disk — neither corrupts the other's view of `~/.life/`. You can flip back and forth safely.

## What to watch for post-cutover

1. **Spec.yaml files that don't progress past `status: ready`** — should never happen; if you see one for >30 min, the sweep cron isn't firing.
2. **Specs at `status: dispatched-subagent` past `watchdog_deadline`** — the watchdog should flip these to `blocked` within one tick. If it doesn't, the sweep cron is broken.
3. **Telegram escalations from the supervisor** — every Run-blocked event sends one. The escalation message includes the §6.3 case (1, 5, or 6) plus the failure reason. Investigate immediately.
4. **`/srv/life/queue.jsonl`** — the curator-style audit trail. Tail it to confirm activity.
