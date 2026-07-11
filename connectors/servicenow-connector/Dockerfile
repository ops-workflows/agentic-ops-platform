FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[connectors]"

COPY shared/ ./shared/
COPY connectors/servicenow-connector/ ./connectors/servicenow-connector/

ENV PYTHONPATH=/app

CMD ["python", "/app/connectors/servicenow-connector/main.py"]
