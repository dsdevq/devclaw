"""Durable deploy hosting — runs a built app on the VPS at a stable Tailscale URL.

When a goal reaches ``achieved``, the owner wants to OPEN the running product at a
stable URL that survives reboots and keeps working without a laptop tunnel held
open. A deploy is:

  * Container ``devclaw-deploy-<slug>`` with ``devclaw.deploy=1`` label.
  * ``--restart unless-stopped``: the deploy comes back after a VPS reboot or a
    docker daemon restart. That is the whole point of "durable".
  * A **deterministic per-slug host port** (not a caller-chosen one): the handoff
    URL must be STABLE across redeploys — every merge to main redeploys the same
    container at the same port, so the link the owner bookmarked keeps working.
  * Reachability is **Tailscale**: the container publishes to ``127.0.0.1:<port>``
    (loopback-only posture, same as the dashboard), and ``tailscale serve --bg
    --https=<port> http://127.0.0.1:<port>`` exposes it as
    ``https://<node>.<tailnet>.ts.net:<port>/`` with auto-TLS. Tailscale-only by
    decision (matches the stack's never-public posture) — no domain, no DNS, no
    public ingress.

Tailscale wiring is **best-effort + graceful-degradation**: ``deploy_project``
ATTEMPTS the serve, and if the ``tailscale`` CLI / tailscaled socket isn't
reachable from devclaw's own container it falls back to returning the exact
one-line command + the resulting URL for a human to run once (the serve config
then persists across reboots, so it's truly one-time-per-app). Mounting the
tailscaled socket into the devclaw-mcp container upgrades this to fully-automatic
with no code change — the seam is already here.

State outside docker + tailscaled is intentionally zero; everything is read back
from the daemons. Tests inject a fake runner so they never touch docker/tailscale.
"""

from __future__ import annotations

import asyncio
import os

from .sandcastle_runner import _translate_workspace_path  # reuse host-path mapping

# In-container launcher. Detects backend/ (FastAPI) and frontend/ (static), serves
# them on ONE origin (so UI + API share a host), and rewrites hard-coded localhost
# API bases in the frontend to same-origin so the deploy works over ANY url. The
# rewrite is done on a /tmp COPY so the mounted workspace is never mutated.
_LAUNCHER = r"""
set -e
cd /app
norm() {
  # copy frontend → writable preview copy, rewrite hard-coded local API bases
  # (http://localhost:8000 / http://127.0.0.1:8000) to same-origin ("" → /path).
  rm -rf /tmp/preview-frontend
  cp -r "$1" /tmp/preview-frontend
  find /tmp/preview-frontend -type f \( -name '*.js' -o -name '*.html' -o -name '*.ts' \) -print0 \
    | xargs -0 -r sed -i -E 's#https?://(localhost|127\.0\.0\.1):8000##g'
}
if [ -f backend/requirements.txt ]; then
  pip install -q -r backend/requirements.txt
  if [ -d frontend ]; then norm frontend; fi
  cd backend
  cat > _preview.py <<'PY'
import os
from main import app
try:
    from fastapi.staticfiles import StaticFiles
    fe = "/tmp/preview-frontend"
    if os.path.isdir(fe):
        # mounted LAST so the app's own routes (/docs, /todos, ...) win and the
        # static mount only catches everything else (serves index.html at /).
        app.mount("/", StaticFiles(directory=fe, html=True), name="ui")
except Exception as e:
    print("preview: static mount skipped:", e)
PY
  exec uvicorn _preview:app --host 0.0.0.0 --port 8000
elif [ -f requirements.txt ]; then
  # Root-level Python ASGI app — FastAPI at app/main.py serving its OWN static UI
  # at /, requirements.txt at the repo root. Detect the ASGI app (module:app) and
  # run it. Falls back to a file listing only if no ASGI app is found.
  pip install -q -r requirements.txt
  target=""
  for cand in app.main main app application src.main app.app; do
    f="$(echo "$cand" | tr . /).py"
    if [ -f "$f" ] && grep -qE 'FastAPI\(|Starlette\(' "$f"; then
      target="$cand:app"; break
    fi
  done
  if [ -n "$target" ]; then
    exec uvicorn "$target" --host 0.0.0.0 --port 8000
  else
    exec python3 -m http.server 8000
  fi
elif [ -d frontend ]; then
  norm frontend
  cd /tmp/preview-frontend && exec python3 -m http.server 8000
else
  exec python3 -m http.server 8000
fi
"""

DEPLOY_IMAGE = os.environ.get("DEVCLAW_DEPLOY_IMAGE") or os.environ.get(
    "DEVCLAW_SANDBOX_IMAGE", "devclaw-sandbox:latest"
)
DOCKER_BIN = os.environ.get("DEVCLAW_DOCKER_BIN", "docker")
TAILSCALE_BIN = os.environ.get("DEVCLAW_TAILSCALE_BIN", "tailscale")
CONTAINER_PORT = 8000
_NAME_PREFIX = "devclaw-deploy-"

# Deterministic per-slug host port range. A deploy's URL must be STABLE across
# redeploys, so the port is derived from the slug (not assigned sequentially or
# read back from a registry). 8200–8399 avoids the preview default (8000) and the
# common dev ports; 200 slots is far more than a single small VPS will host.
DEPLOY_PORT_BASE = int(os.environ.get("DEVCLAW_DEPLOY_PORT_BASE", "8200"))
DEPLOY_PORT_SPAN = int(os.environ.get("DEVCLAW_DEPLOY_PORT_SPAN", "200"))

# Resource governance — deploys are even longer-lived than previews, so the same
# hard per-container caps apply and the count is bounded (oldest-first eviction).
DEPLOY_MEMORY = os.environ.get("DEVCLAW_DEPLOY_MEMORY", "512m")
DEPLOY_CPUS = os.environ.get("DEVCLAW_DEPLOY_CPUS", "1.0")
DEPLOY_MAX = int(os.environ.get("DEVCLAW_DEPLOY_MAX", "5"))


class DeployError(Exception):
    pass


def deploy_name(slug: str) -> str:
    return f"{_NAME_PREFIX}{_safe_slug(slug)}"


def _safe_slug(slug: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in slug).strip("-") or "app"


def deploy_port(slug: str) -> int:
    """Deterministic host port for a slug — stable across redeploys so the handoff
    URL never changes. A simple stable hash over the slug bytes (NOT Python's
    salted ``hash()``, which varies per process) keeps the same slug → same port."""
    safe = _safe_slug(slug)
    acc = 0
    for ch in safe.encode("utf-8"):
        acc = (acc * 31 + ch) & 0xFFFFFFFF
    return DEPLOY_PORT_BASE + (acc % DEPLOY_PORT_SPAN)


async def _run(bin_: str, *args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{bin_} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def _docker(*args: str) -> tuple[int, str]:
    return await _run(DOCKER_BIN, *args)


def _build_run_args(*, name: str, host_path: str, port: int) -> list[str]:
    """Pure docker-run argv for one deploy — unit-testable without docker. Identical
    to a preview except ``--restart unless-stopped`` (durable) + the deploy label."""
    return [
        "run", "-d", "--name", name,
        "--label", "devclaw.deploy=1",
        "--memory", DEPLOY_MEMORY, "--memory-swap", DEPLOY_MEMORY,
        "--cpus", DEPLOY_CPUS,
        # The durability delta: come back after a reboot / daemon restart.
        "--restart", "unless-stopped",
        "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        "-v", f"{host_path}:/app",
        "-e", "OPENHANDS_SUPPRESS_BANNER=1",
        "--entrypoint", "bash",
        DEPLOY_IMAGE,
        "-c", _LAUNCHER,
    ]


async def _container_running(name: str) -> bool:
    rc, out = await _docker("inspect", "-f", "{{.State.Running}}", name)
    return rc == 0 and out.strip() == "true"


async def _ready(name: str, path: str = "/") -> bool:
    """Best-effort readiness — curl the app from INSIDE the container (the published
    port is on the host loopback, unreachable from the devclaw-mcp container). The app
    is "ready" once uvicorn ANSWERS — any HTTP status counts, including 4xx. Probing
    ``/`` (not ``/docs``) avoids a false negative on apps that don't ship Swagger:
    closeloop serves ``/`` 200 but has no ``/docs``. Only a refused/timed-out
    connection (server not up yet) is not-ready."""
    probe = (
        "import urllib.request, urllib.error, sys\n"
        f"try: urllib.request.urlopen('http://localhost:{CONTAINER_PORT}{path}', timeout=3)\n"
        "except urllib.error.HTTPError: pass\n"  # server answered (4xx/5xx) → it's up
        "except Exception: sys.exit(1)\n"        # refused / timeout → not up yet
    )
    rc, _ = await _docker("exec", name, "python3", "-c", probe)
    return rc == 0


async def _evict_to_make_room(keep_name: str) -> list[str]:
    """Stop oldest deploys until starting ``keep_name`` stays within DEPLOY_MAX.
    ``docker ps`` lists newest-first, so reversed() is oldest-first. Also tears down
    the evicted deploy's tailscale serve so the freed port stops 502-ing."""
    rc, out = await _docker("ps", "--filter", "label=devclaw.deploy=1", "--format", "{{.Names}}")
    if rc != 0:
        return []
    names = [n for n in out.splitlines() if n.strip()]
    oldest_first = list(reversed([n for n in names if n != keep_name]))
    evicted: list[str] = []
    while len(oldest_first) + 1 > DEPLOY_MAX:
        victim = oldest_first.pop(0)
        await _docker("rm", "-f", victim)
        if victim.startswith(_NAME_PREFIX):
            await _tailscale_unserve(deploy_port(victim[len(_NAME_PREFIX):]))
        evicted.append(victim)
    return evicted


# ---- Tailscale reachability (best-effort, graceful-degradation) -------------

async def _tailnet_dns_name() -> str | None:
    """The node's MagicDNS name (e.g. ``lifekit-vps.tail1cb676.ts.net``), trailing
    dot stripped. None if tailscale isn't reachable from here.

    NB: ``_run`` folds stderr into stdout, and ``tailscale`` prints a non-fatal
    "Warning: client version != tailscaled server version" line to stderr on a
    version skew — which would prepend non-JSON to the output and break a naive
    ``json.loads``. Slice from the first ``{`` so the warning is tolerated (the
    real cause of the live "DNS name wasn't readable" miss on the first deploy)."""
    rc, out = await _run(TAILSCALE_BIN, "status", "--json")
    if rc != 0:
        return None
    start = out.find("{")
    if start < 0:
        return None
    try:
        import json
        name = json.loads(out[start:]).get("Self", {}).get("DNSName", "")
    except (ValueError, AttributeError):
        return None
    return name.rstrip(".") or None


async def _tailscale_serve(port: int) -> bool:
    """Expose host loopback ``port`` on the tailnet at the same HTTPS port. Idempotent
    (re-running just re-asserts the mapping). Returns False if tailscale isn't usable
    from here — the caller then surfaces the manual one-liner instead."""
    rc, _ = await _run(
        TAILSCALE_BIN, "serve", "--bg",
        f"--https={port}", f"http://127.0.0.1:{port}",
    )
    return rc == 0


async def _tailscale_unserve(port: int) -> None:
    await _run(TAILSCALE_BIN, "serve", "--https", str(port), "off")


def serve_command(port: int) -> str:
    """The exact one-time host command to expose a deploy over Tailscale. Persists
    across reboots once run, so it's one-time-per-app (not per-deploy)."""
    return f"tailscale serve --bg --https={port} http://127.0.0.1:{port}"


def _urls(port: int, dns_name: str | None) -> dict:
    """Public (Tailscale https) URL if we know the node name, plus the always-true
    loopback URL + SSH-tunnel fallback so a deploy is reachable even pre-serve."""
    loopback = f"http://localhost:{port}"
    out = {
        "loopback_url": f"{loopback}/",
        "loopback_api_docs": f"{loopback}/docs",
        "tunnel": f"ssh -L {port}:127.0.0.1:{port} lifekit-vps",
    }
    if dns_name:
        base = f"https://{dns_name}:{port}"
        out.update({"url": f"{base}/", "api_docs_url": f"{base}/docs", "api_url": base})
    return out


# ---- public surface ---------------------------------------------------------

async def deploy_project(workspace_dir: str, slug: str) -> dict:
    """Run a project's built app as a DURABLE, reboot-surviving deploy and return its
    stable URL. Replaces any existing deploy for the slug (idempotent redeploy at the
    same port → same URL) and evicts the oldest deploy if at the VPS cap. Attempts to
    wire Tailscale; on failure returns the one-time serve command to run by hand.
    Raises :class:`DeployError` if the container itself can't be started."""
    name = deploy_name(slug)
    port = deploy_port(slug)
    host_path = _translate_workspace_path(workspace_dir)

    await _docker("rm", "-f", name)  # idempotent redeploy
    evicted = await _evict_to_make_room(name)

    rc, out = await _docker(*_build_run_args(name=name, host_path=host_path, port=port))
    if rc != 0:
        raise DeployError(f"failed to start deploy: {out[-400:]}")

    ready = False
    for _ in range(30):  # ~30s: pip install + uvicorn boot (deploys may be heavier than previews)
        await asyncio.sleep(1)
        if not await _container_running(name):
            _, logs = await _docker("logs", "--tail", "40", name)
            raise DeployError(f"deploy container exited during startup:\n{logs[-600:]}")
        if await _ready(name):
            ready = True
            break

    served = await _tailscale_serve(port)
    dns_name = await _tailnet_dns_name() if served else None
    urls = _urls(port, dns_name)

    if served and dns_name:
        note = "deploy is live — open `url`. Survives reboots; every merge redeploys this same URL."
    elif served:
        note = ("tailscale serve is on but the node DNS name wasn't readable — the deploy is at "
                f"https://<this-node>.<tailnet>.ts.net:{port}/")
    else:
        note = ("deploy is up on the host loopback; run this once on lifekit-vps to expose it over "
                f"Tailscale (persists across reboots): {serve_command(port)}")

    return {
        "slug": slug, "container": name, "port": port, "ready": ready,
        "tailscale_served": served, "evicted": evicted,
        "serve_command": serve_command(port),
        **urls,
        "note": note,
    }


async def stop_deploy(slug: str) -> dict:
    name = deploy_name(slug)
    port = deploy_port(slug)
    rc, out = await _docker("rm", "-f", name)
    await _tailscale_unserve(port)
    return {"slug": slug, "container": name, "stopped": rc == 0, "detail": out[-200:] if rc else ""}


async def deploy_status(slug: str) -> dict:
    name = deploy_name(slug)
    port = deploy_port(slug)
    rc, out = await _docker("inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name)
    if rc != 0:
        return {"slug": slug, "container": name, "exists": False, "port": port}
    status, running = (out.split("|", 1) + ["", ""])[:2]
    is_running = running.strip() == "true"
    ready = await _ready(name) if is_running else False
    dns_name = await _tailnet_dns_name()
    return {
        "slug": slug, "container": name, "exists": True,
        "status": status.strip(), "running": is_running, "ready": ready,
        "port": port, "serve_command": serve_command(port), **_urls(port, dns_name),
    }


async def list_deploys() -> list[dict]:
    rc, out = await _docker(
        "ps", "-a", "--filter", "label=devclaw.deploy=1",
        "--format", "{{.Names}}\t{{.Status}}",
    )
    if rc != 0 or not out.strip():
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        nm = parts[0]
        rows.append({
            "container": nm,
            "slug": nm[len(_NAME_PREFIX):] if nm.startswith(_NAME_PREFIX) else nm,
            "status": parts[1] if len(parts) > 1 else "",
        })
    return rows
