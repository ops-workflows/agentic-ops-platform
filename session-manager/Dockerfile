FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends tar && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev,connectors]"

# Copy shared library and session manager
COPY shared/ ./shared/
COPY CLAUDE.md ./CLAUDE.md
COPY skills/ ./skills/
COPY hooks/ ./hooks/
COPY mcps/ ./mcps/
COPY gateway/ ./gateway/
COPY session-manager/ ./session_manager/
COPY examples/workflow-repo/platform-config.example.yaml ./platform-config.example.yaml
RUN mkdir -p /app/workflows

# Create volumes directory
RUN mkdir -p /volumes

CMD ["python", "-m", "session_manager.main"]
