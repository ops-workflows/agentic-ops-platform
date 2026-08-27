FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[knowledge-indexer]"

COPY shared/ ./shared/
COPY knowledge-indexer/ ./knowledge_indexer/

RUN mkdir -p /var/lib/agentic-ops/knowledge-indexer

CMD ["python", "-m", "knowledge_indexer.main"]
