# Orchestrator image — pre-warms the sandcastle CLI so first-task latency
# doesn't include a ~30s npx tarball pull.
#
# In B-5 deployment the actually-running image is built from
# dsdevq/lifekit-stack `compose/openclaw-gateway/Dockerfile`; this Dockerfile
# is the canonical reference for what that image MUST include for the
# sandcastle adapter to work. Bumping `SANDCASTLE_VERSION` here MUST be
# matched by a bump in `src/orchestrator/config/sandbox.yaml`.

FROM node:20-bookworm-slim AS base

ARG SANDCASTLE_VERSION=0.5.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI — sandcastle shells out to `docker` from inside this image to
# create the per-task containers. The Docker DAEMON is reached via the host
# socket bind-mounted at /var/run/docker.sock (DinD pattern).
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# gh CLI — the agent (inside the sandcastle container) uses gh to open the PR.
# Pre-installed in the orchestrator image too so the in-process safety-net
# adapter still works without surprises.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── pre-warm sandcastle ──────────────────────────────────────────────────────
# One-liner that pulls the npm tarball into the npx cache layer. The `--help`
# invocation forces sandcastle's entrypoint to run once so any postinstall
# scripts have already completed by the time the daemon starts.
# DO NOT remove without also extending the sandcastle.run timeout — first-run
# latency without this prewarm is ~30s and will time-bomb short timeouts.
RUN npx -y sandcastle@${SANDCASTLE_VERSION} --help \
    || echo "WARN: sandcastle prewarm failed; first task will pay the cold-start cost"

WORKDIR /app

# The orchestrator package itself is installed (pip-editable from a bind mount)
# by the lifekit-stack entrypoint at container start — see DEPLOY.md. We
# intentionally do NOT copy the source tree here; that keeps this image
# orchestrator-version-agnostic and avoids rebuilds on pure-Python edits.

CMD ["devclaw-orchestrator", "daemon", "--life", "/home/node/.life"]
