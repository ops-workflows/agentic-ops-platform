FROM python:3.11-slim

LABEL maintainer="ai-ops-platform"
LABEL description="Agentic Ops runtime — ephemeral container for agent sessions"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap \
    curl \
    git \
    socat \
    tar \
    jq \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js + Claude Code CLI ──────────────────────────────────────
# The claude-agent-sdk Python package spawns the `claude` CLI binary
# as a subprocess (via anyio.open_process). The CLI is an npm package
# (@anthropic-ai/claude-code) that requires Node.js.
# Pre-bake it so containers don't download on every start.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code@2.1.201 \
    && rm -rf /var/lib/apt/lists/*

# Verify claude CLI is available
RUN claude --version

# Python dependencies (includes claude-agent-sdk)
COPY runtime/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy entrypoint and the shared harness library. Workflow content (plugin
# dir or workflow bundle) is provided at runtime, not baked in, but shared/lib
# is harness code the entrypoint imports directly, so it must always be
# present regardless of deployment target (Docker bind mount, Kubernetes,
# Cloud Run) or bundle transport (local path, file://, https://).
COPY runtime/session_entrypoint.py /entrypoint.py
COPY shared/ /shared/

ENV PYTHONPATH=/

# Plugin source is mounted read-only at /plugin-src by container_lifecycle.py.
# The entrypoint stages a writable workspace at /workspace before starting
# Claude.  /memory is the persistent agent-memory Docker volume. A host
# shared/ directory may still be bind-mounted over /shared for local
# development so live edits don't require an image rebuild.
RUN mkdir -p /plugin-src /workspace /memory

WORKDIR /workspace

CMD ["python", "/entrypoint.py"]
