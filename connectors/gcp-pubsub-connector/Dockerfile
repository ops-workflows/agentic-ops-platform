FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[connectors]"

COPY shared/ ./shared/
COPY connectors/gcp-pubsub-connector/ ./connectors/gcp-pubsub-connector/

ENV PYTHONPATH=/app

CMD ["python", "/app/connectors/gcp-pubsub-connector/main.py"]