FROM python:3.11-slim

WORKDIR /app
COPY mcps/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY mcps/ ./mcps/

ENV PYTHONPATH=/app

# Entrypoint set per-service via docker-compose command
CMD ["python", "-m", "uvicorn", "mcps.core.mcp_message:app", "--host", "0.0.0.0", "--port", "8100"]
