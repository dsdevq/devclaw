# devclaw v2 — per-task sandbox image.
#
# One container per task. The host TaskQueue does `docker run --rm` against
# this image, bind-mounts the task's workspace_dir at /workspace and the
# host's ~/.claude read-only at /home/agent/.claude, then exec's runner.py.
# Container vaporizes on exit.
#
# Image is rebuilt whenever python-runner/ or this Dockerfile changes.
# Build from the v2/ directory:
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

# Install python deps system-wide so the agent user can import them without
# its own venv. openhands-sdk pulls in a long tail of deps; the image is fat
# but that's fine — it's built once per sandbox version.
COPY python-runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Claude CLI + claude-agent-acp via npm. Pinned so image is reproducible.
RUN npm install -g \
      @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
      @agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION} \
 && npm cache clean --force

COPY --chown=agent:agent python-runner/runner.py /opt/devclaw/runner.py

USER agent
WORKDIR /workspace

ENV CLAUDE_CODE_EXECUTABLE=/usr/bin/claude \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    HOME=/home/agent \
    PATH=/usr/local/bin:/usr/bin:/bin

ENTRYPOINT ["python3", "/opt/devclaw/runner.py"]
