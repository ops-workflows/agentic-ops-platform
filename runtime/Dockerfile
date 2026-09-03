FROM python:3.11-slim AS bubblewrap-builder

ARG BUBBLEWRAP_VERSION=0.11.0
ARG BUBBLEWRAP_SHA256=988fd6b232dafa04b8b8198723efeaccdb3c6aa9c1c7936219d5791a8b7a8646

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        curl \
        libcap-dev \
        meson \
        ninja-build \
        pkg-config \
        xz-utils \
        && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
            "https://github.com/containers/bubblewrap/releases/download/v${BUBBLEWRAP_VERSION}/bubblewrap-${BUBBLEWRAP_VERSION}.tar.xz" \
            -o /tmp/bubblewrap.tar.xz \
        && echo "${BUBBLEWRAP_SHA256}  /tmp/bubblewrap.tar.xz" | sha256sum -c - \
        && tar -xJf /tmp/bubblewrap.tar.xz -C /tmp \
        && meson setup /tmp/bubblewrap-build "/tmp/bubblewrap-${BUBBLEWRAP_VERSION}" \
            --prefix=/usr \
            -Dbash_completion=disabled \
            -Dman=disabled \
            -Dselinux=disabled \
            -Dtests=false \
            -Dzsh_completion=disabled \
        && meson compile -C /tmp/bubblewrap-build

FROM python:3.11-slim

LABEL maintainer="ai-ops-platform"
LABEL description="Agentic Ops runtime — ephemeral container for agent sessions"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    libcap2 \
    socat \
    tar \
    jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bubblewrap-builder /tmp/bubblewrap-build/bwrap /usr/bin/bwrap
RUN test "$(bwrap --version)" = "bubblewrap 0.11.0"

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
