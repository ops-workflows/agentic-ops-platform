FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]"

# Copy shared library and gateway
COPY shared/ ./shared/
COPY CLAUDE.md ./CLAUDE.md
COPY skills/ ./skills/
COPY hooks/ ./hooks/
COPY mcps/ ./mcps/
COPY schemas/ ./schemas/
COPY gateway/ ./gateway/
COPY session-manager/ ./session_manager/
COPY connectors/ ./connectors/
COPY examples/workflow-repo/platform-config.example.yaml ./platform-config.example.yaml
RUN mkdir -p /app/workflows

EXPOSE 8080

# The gateway embeds an in-memory APScheduler, so it must run as a single
# process until scheduling is moved into its own dedicated service/job store.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
