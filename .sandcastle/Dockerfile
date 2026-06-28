# devclaw v2 — per-task sandbox image.
#
# One container per task. The host TaskQueue does `docker run --rm` against
# this image, bind-mounts the task's workspace_dir at /workspace and the
# host's ~/.claude read-only at /home/agent/.claude, then exec's runner.py.
# Container vaporizes on exit.
#
# Image is rebuilt whenever openhands-runner/ or this Dockerfile changes.
# Build from the repo root:
#   docker build -t devclaw-sandbox:latest -f .sandcastle/Dockerfile .

FROM python:3.13-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000
# Claude CLI version pinned to match what the host runs (2.x line).
ARG CLAUDE_CLI_VERSION=2.1.139
ARG CLAUDE_AGENT_ACP_VERSION=0.37.0

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Node 20 (for claude CLI + claude-agent-acp), git for whatever the agent does,
# ca-certificates so https works, plus a non-root agent user matching host UID.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid ${AGENT_GID} agent \
 && useradd  --uid ${AGENT_UID} --gid ${AGENT_GID} --create-home --shell /bin/bash agent

# Headless-Chromium system libraries required by the Playwright MCP server the
# agent calls when it needs to drive a real browser (UI verification, E2E).
# Same list openclaw-gateway uses — without these the bundled Chromium fails to
# launch on debian-slim with cryptic "shared libraries" errors.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libxcomposite1 libxdamage1 libatspi2.0-0 libdrm2 libxkbcommon0 \
      libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
 && rm -rf /var/lib/apt/lists/*

# Install python deps system-wide so the agent user can import them without
# its own venv. openhands-sdk pulls in a long tail of deps; the image is fat
# but that's fine — it's built once per sandbox version.
COPY openhands-runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Claude CLI + claude-agent-acp via npm. Pinned so image is reproducible.
# @playwright/mcp is the MCP server the agent reaches Playwright through
# (configured at /workspace/.mcp.json by runner.py per task).
RUN npm install -g \
      @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
      @agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION} \
      @playwright/mcp@latest \
 && npm cache clean --force

# Bake the sandbox-only MCP config. runner.py copies this to
# /workspace/.mcp.json at task start so claude auto-discovers Playwright MCP
# WITHOUT mounting the host's mcpServers (which would re-open the leak
# SANDBOX_CLAUDE_ALLOWLIST exists to prevent).
COPY --chown=agent:agent openhands-runner/sandbox-mcp.json /opt/devclaw/sandbox-mcp.json

COPY --chown=agent:agent openhands-runner/runner.py /opt/devclaw/runner.py
# Skill bundle + hooks baked into the image. Skills are markdown files
# runner.py concatenates per-task-kind and prepends to the goal; hooks are
# best-effort bash scripts runner.py invokes around the agent loop.
COPY --chown=agent:agent openhands-runner/skills/ /opt/devclaw/skills/
COPY --chown=agent:agent openhands-runner/hooks/  /opt/devclaw/hooks/
RUN chmod +x /opt/devclaw/hooks/*.sh

USER agent

# Pre-fetch the Playwright Chromium build into the agent user's cache so the
# Playwright MCP server doesn't pull ~150MB on first invocation (and so first
# call works without network). Lands in /home/agent/.cache/ms-playwright.
RUN npx -y playwright@latest install chromium

WORKDIR /workspace

ENV CLAUDE_CODE_EXECUTABLE=/usr/bin/claude \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    HOME=/home/agent \
    PATH=/usr/local/bin:/usr/bin:/bin

ENTRYPOINT ["python3", "/opt/devclaw/runner.py"]
