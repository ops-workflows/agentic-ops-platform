FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
	&& apt-get install --no-install-recommends -y git \
	&& rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[connectors]"

COPY shared/ ./shared/
COPY connectors/servicenow-connector/ ./connectors/servicenow-connector/

ENV PYTHONPATH=/app

CMD ["python", "/app/connectors/servicenow-connector/main.py"]
