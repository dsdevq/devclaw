"""Live preview hosting — run a built app on the VPS and hand back clickable links.

devclaw's job isn't done when the PR merges; the owner wants to *open the thing*.
This module runs a project's built app in a long-lived container and reports the
URLs: the API docs (FastAPI ships Swagger at /docs for free) and the frontend.

Design (v1, convention-based — matches what devclaw builds from scratch):
  * One detached container per project, ``devclaw-preview-<slug>``, reusing the
    sandbox image (it already has python3 + uvicorn + node). NOT --rm: a preview
    is meant to stay up so you can keep clicking it. Replacing a preview just
    re-runs with the same name.
  * The workspace is mounted at /app. If a ``backend/`` (FastAPI) is present we
    pip-install it and serve it with uvicorn; if a ``frontend/`` is present we
    mount it as static files ONTO THE SAME FastAPI app at ``/`` — so the frontend
    and API share one origin/port. That makes a hard-coded ``localhost:8000``
    API base in the frontend "just work" behind a single SSH tunnel, and needs
    no CORS. A static-only project falls back to ``python -m http.server``.
  * The container's :8000 is published to ``127.0.0.1:<port>`` on the HOST (same
    loopback-only posture as the dashboard) so it's reachable via an SSH tunnel:
        ssh -L 8000:127.0.0.1:8000 lifekit-vps  →  open http://localhost:8000/
  * State is DERIVED from docker (container name + published port via inspect) —
    no separate registry to rot.

State outside docker is intentionally zero; everything is read back from the
daemon. Tests inject a fake runner so they never need docker.
"""

from __future__ import annotations

import asyncio
import json
import os

from .sandcastle_runner import _translate_workspace_path  # reuse host-path mapping

PREVIEW_IMAGE = os.environ.get("DEVCLAW_PREVIEW_IMAGE") or os.environ.get(
    "DEVCLAW_SANDBOX_IMAGE", "devclaw-sandbox:latest"
)
DOCKER_BIN = os.environ.get("DEVCLAW_DOCKER_BIN", "docker")
# Default host port. 8000 by design: the from-scratch frontend devclaw builds
# tends to hard-code http://localhost:8000 as its API base, so publishing the
# preview there lets `ssh -L 8000:127.0.0.1:8000` make that base resolve.
DEFAULT_PORT = int(os.environ.get("DEVCLAW_PREVIEW_PORT", "8000"))
CONTAINER_PORT = 8000
_NAME_PREFIX = "devclaw-preview-"

# Resource governance — the VPS is small, and previews are LONG-LIVED, so they
# must not pile up and OOM the host. Each preview is hard-capped, and the number
# of concurrent previews is bounded with oldest-first eviction.
PREVIEW_MEMORY = os.environ.get("DEVCLAW_PREVIEW_MEMORY", "512m")
PREVIEW_CPUS = os.environ.get("DEVCLAW_PREVIEW_CPUS", "1.0")
PREVIEW_MAX = int(os.environ.get("DEVCLAW_PREVIEW_MAX", "3"))

# The in-container launcher. Detects backend/ (FastAPI) and frontend/ (static),
# serves them on ONE origin so a hard-coded API base + the UI share localhost:8000.
_LAUNCHER = r"""
set -e
cd /app
if [ -f backend/requirements.txt ]; then
  pip install -q -r backend/requirements.txt
  cd backend
  cat > _preview.py <<'PY'
import os
from main import app
try:
    from fastapi.staticfiles import StaticFiles
    fe = "/app/frontend"
    if os.path.isdir(fe):
        # mounted LAST so the app's own routes (/docs, /todos, ...) win and the
        # static mount only catches everything else (serves index.html at /).
        app.mount("/", StaticFiles(directory=fe, html=True), name="ui")
except Exception as e:
    print("preview: static mount skipped:", e)
PY
  exec uvicorn _preview:app --host 0.0.0.0 --port 8000
elif [ -d frontend ]; then
  cd frontend && exec python3 -m http.server 8000
else
  exec python3 -m http.server 8000
fi
"""


class PreviewError(Exception):
    pass


def preview_name(slug: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in slug).strip("-") or "app"
    return f"{_NAME_PREFIX}{safe}"


async def _run(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{DOCKER_BIN} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


def _build_run_args(*, name: str, host_path: str, port: int) -> list[str]:
    """Pure docker-run argv for one preview — unit-testable without docker."""
    return [
        "run", "-d", "--name", name,
        "--label", "devclaw.preview=1",
        # Hard resource caps so one preview can't OOM the small VPS. --memory-swap
        # = --memory disables swap growth (a true ceiling, not soft).
        "--memory", PREVIEW_MEMORY, "--memory-swap", PREVIEW_MEMORY,
        "--cpus", PREVIEW_CPUS,
        "--restart", "no",  # a crashed preview frees its slot instead of looping
        "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        "-v", f"{host_path}:/app",
        "-e", "OPENHANDS_SUPPRESS_BANNER=1",
        "--entrypoint", "bash",
        PREVIEW_IMAGE,
        "-c", _LAUNCHER,
    ]


def _urls(port: int) -> dict:
    base = f"http://localhost:{port}"
    return {"frontend_url": f"{base}/", "api_docs_url": f"{base}/docs", "api_url": base}


async def _container_running(name: str) -> bool:
    rc, out = await _run("inspect", "-f", "{{.State.Running}}", name)
    return rc == 0 and out.strip() == "true"


async def _ready(name: str, path: str = "/docs") -> bool:
    """Best-effort readiness: curl the app from INSIDE the container (the published
    port lives on the host loopback, unreachable from the devclaw-mcp container)."""
    rc, _ = await _run(
        "exec", name, "python3", "-c",
        f"import urllib.request; urllib.request.urlopen('http://localhost:{CONTAINER_PORT}{path}', timeout=3)",
    )
    return rc == 0


async def _evict_to_make_room(keep_name: str) -> list[str]:
    """Stop oldest previews until starting ``keep_name`` keeps us within
    PREVIEW_MAX. `docker ps` lists newest-first, so reversed() is oldest-first.
    Resource guard: bounds how many long-lived previews coexist on the VPS."""
    rc, out = await _run("ps", "--filter", "label=devclaw.preview=1", "--format", "{{.Names}}")
    if rc != 0:
        return []
    names = [n for n in out.splitlines() if n.strip()]
    oldest_first = list(reversed([n for n in names if n != keep_name]))
    evicted: list[str] = []
    while len(oldest_first) + 1 > PREVIEW_MAX:  # +1 = the preview we're about to start
        victim = oldest_first.pop(0)
        await _run("rm", "-f", victim)
        evicted.append(victim)
    return evicted


async def start_preview(workspace_dir: str, slug: str, *, port: int = DEFAULT_PORT) -> dict:
    """Run the project's built app as a long-lived preview and return its URLs.
    Replaces any existing preview with the same slug, and evicts the oldest
    preview(s) if we're at the PREVIEW_MAX concurrency cap (VPS resource guard).
    Returns a verdict dict; raises :class:`PreviewError` if it can't be started."""
    name = preview_name(slug)
    host_path = _translate_workspace_path(workspace_dir)

    # Replace an existing preview (idempotent restart), then bound concurrency.
    await _run("rm", "-f", name)
    evicted = await _evict_to_make_room(name)

    rc, out = await _run(*_build_run_args(name=name, host_path=host_path, port=port))
    if rc != 0:
        raise PreviewError(f"failed to start preview: {out[-400:]}")

    # Give the app a moment, then check it didn't immediately crash + probe readiness.
    ready = False
    for _ in range(20):  # ~20s: pip install + uvicorn boot
        await asyncio.sleep(1)
        if not await _container_running(name):
            _, logs = await _run("logs", "--tail", "40", name)
            raise PreviewError(f"preview container exited during startup:\n{logs[-600:]}")
        if await _ready(name):
            ready = True
            break

    return {
        "slug": slug, "container": name, "port": port, "ready": ready,
        "evicted": evicted,  # older previews stopped to stay within the resource cap
        **_urls(port),
        "tunnel": f"ssh -L {port}:127.0.0.1:{port} lifekit-vps",
        "note": (
            "open the frontend_url after starting the SSH tunnel; api_docs_url is the Swagger UI"
            if ready else
            "container is up but not answering yet — give it a few more seconds, then check preview_status"
        ),
    }


async def stop_preview(slug: str) -> dict:
    name = preview_name(slug)
    rc, out = await _run("rm", "-f", name)
    return {"slug": slug, "container": name, "stopped": rc == 0, "detail": out[-200:] if rc else ""}


async def preview_status(slug: str) -> dict:
    name = preview_name(slug)
    rc, out = await _run("inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name)
    if rc != 0:
        return {"slug": slug, "container": name, "exists": False}
    status, running = (out.split("|", 1) + ["", ""])[:2]
    port = DEFAULT_PORT
    rc2, pout = await _run("port", name, f"{CONTAINER_PORT}/tcp")
    if rc2 == 0 and ":" in pout:
        try:
            port = int(pout.strip().rsplit(":", 1)[-1])
        except ValueError:
            pass
    ready = await _ready(name) if running.strip() == "true" else False
    return {
        "slug": slug, "container": name, "exists": True,
        "status": status.strip(), "running": running.strip() == "true",
        "ready": ready, "port": port, **_urls(port),
    }


async def list_previews() -> list[dict]:
    rc, out = await _run(
        "ps", "-a", "--filter", "label=devclaw.preview=1",
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
