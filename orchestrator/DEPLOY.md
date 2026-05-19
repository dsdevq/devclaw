# Deploying `devclaw-orchestrator` to the VPS (B-5 architecture)

Operational runbook for the **B-5 long-running container** deployment of the orchestrator. As of 2026-05-18 the orchestrator no longer runs as a host-local venv driven by markdown-cron entries — it runs as a long-lived Docker container managed by the `lifekit-stack` compose project, and its sweep + supervise loops are internal Python loops inside that daemon process.

> **Historical note.** The prior runbook (PR #10) installed the orchestrator into `/opt/devclaw-orchestrator/.venv` on the VPS host and registered `openclaw cron add sweep_15m` / `supervise_30m` entries. **That deploy model is retired.** The B-5 cutover replaced it with a container daemon. The matching markdown-cron entries (`task_dispatch_15m`, `curator_30m`) are now disabled in `jobs.json` and must stay disabled while the container is the source of truth — running both races over `~/.life/tasks/*/spec.yaml` and double-dispatches.

## Architecture summary

- **Container**: `compose-lifekit-orchestrator-1`
- **Image**: `lifekit-openclaw:local`
- **Image source**: `dsdevq/lifekit-stack` → `compose/openclaw-gateway/Dockerfile`
- **Service definition**: `dsdevq/lifekit-stack` → `compose/docker-compose.yml`, under `services.lifekit-orchestrator`
- **Entrypoint behaviour**: at every container start, the entrypoint installs the orchestrator as a pip-editable package from the bind-mounted source tree at `/home/node/.openclaw/workspace/devclaw/orchestrator`. This is why a `git pull` on the host followed by `docker restart` is enough to roll out code changes — no rebuild required for orchestrator-only edits.
- **Command**: `devclaw-orchestrator daemon --life /home/node/.life --telegram-chat $LIFEKIT_TELEGRAM_CHAT`
- **Loops**: `sweep` (~every 15 min) and `supervise` (~every 30 min) run as in-process Python loops inside the daemon. They are **not** scheduled by openclaw-cron and have no corresponding `openclaw cron` entries.
- **Killswitch**: `/srv/life/system/cron-paused` — the daemon loops short-circuit when this file exists, matching the markdown-cron contract.

## Pre-flight

On the VPS host (assumes `denys@<vps>` SSH access via Tailscale):

- [ ] `docker --version` and `docker compose version` available
- [ ] `gh auth status` is OK (push access to target repos)
- [ ] `claude --version` runs cleanly and reports an authenticated session under `~/.claude/` (the container mounts this in for the subagent dispatch path)
- [ ] `/srv/life/` (canonical) is reachable and writable on the host — it bind-mounts into the container as `/home/node/.life`
- [ ] `dsdevq/lifekit-stack` and `dsdevq/devclaw` are both checked out at the canonical host paths used by the compose file (typically `/srv/openclaw/lifekit-stack` and `/srv/openclaw/workspace/devclaw`)

## Step 1 — Build the `lifekit-openclaw:local` image

The image is defined in the `lifekit-stack` repo, not in `devclaw`. Build it (or rebuild it whenever the Dockerfile, base image, or installed system deps change):

```bash
ssh denys@<vps>
cd /srv/openclaw/lifekit-stack/compose
docker compose build lifekit-orchestrator
```

This produces (or updates) the local image tagged `lifekit-openclaw:local`. The Dockerfile lives at `compose/openclaw-gateway/Dockerfile` and is shared with the gateway service.

> You only need this rebuild step for **image-level** changes (system packages, Python base, Dockerfile edits). Pure orchestrator code changes do **not** require a rebuild — see "Rolling deploys" below.

## Step 2 — Configure environment

Add `LIFEKIT_TELEGRAM_CHAT` (the chat ID the supervisor escalates to) to the compose `.env` file:

```bash
cd /srv/openclaw/lifekit-stack/compose
# Append if missing; replace <chat-id> with the real numeric chat id.
grep -q '^LIFEKIT_TELEGRAM_CHAT=' .env || echo 'LIFEKIT_TELEGRAM_CHAT=<chat-id>' >> .env
```

The compose service passes this through as `$LIFEKIT_TELEGRAM_CHAT` on the `devclaw-orchestrator daemon` command line. Restart is required for changes to take effect (see "Rolling deploys").

## Step 3 — Bring up the long-running container

```bash
cd /srv/openclaw/lifekit-stack/compose
docker compose up -d lifekit-orchestrator
```

This starts (or recreates, if compose detects config drift) `compose-lifekit-orchestrator-1`. First start does the editable install from the bind-mounted devclaw source tree; logs print `Successfully installed devclaw-orchestrator-…` followed by the daemon banner.

## Step 4 — Verify the container is healthy

The compose service ships with a healthcheck (added in `lifekit-stack#11`). Confirm:

```bash
docker ps --filter name=lifekit-orchestrator --format '{{.Names}}\t{{.Status}}'
```

Expected: `compose-lifekit-orchestrator-1   Up <duration> (healthy)`.

If the container is `(unhealthy)` or `(health: starting)` for >2 minutes, inspect the last check:

```bash
docker inspect --format='{{json .State.Health}}' compose-lifekit-orchestrator-1 | jq
```

Sanity-check from inside the container:

```bash
docker exec compose-lifekit-orchestrator-1 devclaw-orchestrator --help
docker exec compose-lifekit-orchestrator-1 devclaw-orchestrator sweep --life /home/node/.life --quiet
```

`sweep` should exit 0 and report `scanned=N dispatched=0 reaped=0 ghosted=0 errors=0` against a clean queue.

## Step 5 — Confirm the markdown-cron disable

The cutover requires the legacy markdown-cron jobs to stay disabled in `jobs.json`. Verify they are not active:

```bash
sudo -u lifekit openclaw cron list | grep -E 'task_dispatch_15m|curator_30m'
```

Both entries should be marked disabled (or absent). If either is enabled, disable it:

```bash
sudo -u lifekit openclaw cron disable task_dispatch_15m
sudo -u lifekit openclaw cron disable curator_30m
```

There must be **no `openclaw cron` entries** for the orchestrator (`sweep_15m`, `supervise_30m`, etc.). The previous runbook added those; the B-5 daemon owns them now and concurrent host-cron entries would double-dispatch.

## Killswitch operations

Pause all orchestrator activity (matches the legacy markdown contract):

```bash
ssh denys@<vps>
touch /srv/life/system/cron-paused
```

The internal `sweep` and `supervise` loops detect this file at the top of each tick and skip the cycle without progressing any specs. The container stays running and healthy.

Resume:

```bash
rm /srv/life/system/cron-paused
```

The next tick (within ~15 min for sweep, ~30 min for supervise) resumes normal processing. No restart required.

## Rolling deploys

**Orchestrator code change (Python source under `orchestrator/`)** — no rebuild:

```bash
ssh denys@<vps>
sudo -u lifekit git -C /srv/openclaw/workspace/devclaw pull
docker restart compose-lifekit-orchestrator-1
```

On restart the entrypoint re-runs the editable install from the now-updated source tree and the daemon comes back on the new code.

**Compose-level change (service config, env, image dep)** — force-recreate:

```bash
cd /srv/openclaw/lifekit-stack/compose
sudo -u lifekit git pull        # if the change is in lifekit-stack
docker compose up -d --force-recreate lifekit-orchestrator
```

**Image-level change (Dockerfile, base image, system packages)** — rebuild + recreate:

```bash
cd /srv/openclaw/lifekit-stack/compose
docker compose build lifekit-orchestrator
docker compose up -d --force-recreate lifekit-orchestrator
```

## Logs and observability

Container-level logs (daemon stdout/stderr — sweep/supervise tick summaries, escalations, daemon banner):

```bash
docker logs -f compose-lifekit-orchestrator-1
docker logs --since 1h compose-lifekit-orchestrator-1 | grep -E 'sweep|supervise|escalat'
```

Per-task dispatch logs (subagent stdout/stderr, one file per task run):

```bash
ls /srv/life/projects/*/tasks/*/dispatch.log
tail -F /srv/life/projects/<project>/tasks/<task>/dispatch.log
```

`/srv/life/queue.jsonl` is the curator-style audit trail — `tail -F` it to watch sweep/supervise activity in real time.

Health snapshot:

```bash
docker inspect --format='{{.State.Health.Status}}' compose-lifekit-orchestrator-1
```

## Rollback

Code rollback (revert a bad orchestrator commit and restart — no rebuild):

```bash
ssh denys@<vps>
sudo -u lifekit git -C /srv/openclaw/workspace/devclaw revert <bad-sha>
sudo -u lifekit git -C /srv/openclaw/workspace/devclaw push
docker restart compose-lifekit-orchestrator-1
```

Or pin to a known-good ref:

```bash
sudo -u lifekit git -C /srv/openclaw/workspace/devclaw checkout <good-sha>
docker restart compose-lifekit-orchestrator-1
```

Compose / image rollback (revert the lifekit-stack change and recreate):

```bash
cd /srv/openclaw/lifekit-stack
sudo -u lifekit git revert <bad-sha> && sudo -u lifekit git push
cd compose
docker compose up -d --force-recreate lifekit-orchestrator
```

Full stop (kill the container, leave markdown-cron disabled — orchestrator becomes a no-op until brought back):

```bash
cd /srv/openclaw/lifekit-stack/compose
docker compose stop lifekit-orchestrator
touch /srv/life/system/cron-paused   # belt-and-braces; daemon is stopped anyway
```

Re-enabling the legacy markdown crons (`task_dispatch_15m`, `curator_30m`) is **not** a supported rollback path under B-5 — those jobs have not been exercised since the cutover and may have drifted from the current spec.yaml shape. If you genuinely need the legacy path back, treat it as a separate cutover and validate on a clean queue first.

## What to watch for post-deploy

1. **`compose-lifekit-orchestrator-1` flipping to `(unhealthy)`** — the healthcheck from lifekit-stack#11 is the first signal something is wrong. `docker logs` and `docker inspect ... .State.Health` together usually identify it.
2. **Spec.yaml files stuck at `status: ready` for >30 min** — sweep loop isn't ticking. Check daemon logs for a stuck loop or an uncaught exception that killed the loop task.
3. **Specs at `status: dispatched-subagent` past `watchdog_deadline`** — supervise loop should flip these to `blocked` within one tick.
4. **Telegram escalations from the supervisor** — every Run-blocked event sends one to `$LIFEKIT_TELEGRAM_CHAT`. Investigate immediately.
5. **`/srv/life/queue.jsonl` going silent** — combined with a healthy container this means the killswitch is on, or the bind mount drifted. Check `/srv/life/system/cron-paused` and the container's mount table (`docker inspect ... .Mounts`).
