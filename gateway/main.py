"""Gateway — main FastAPI application.

The Gateway is the web-facing HTTP service. It receives all inbound requests,
writes to Postgres, and returns. No heavy processing — it's a thin routing layer.

Mounts:
    - Message webhook handler (human → agent)
  - Event collector (agent → control plane, replaces Langfuse)
  - Control-plane API (UI data)
  - Scheduler (APScheduler for cron jobs)
  - Provisioner (agent discovery & registration)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.api import router as api_router
from gateway.event_collector import router as event_router
from gateway.message import router as message_router
from gateway.provisioner import run_provisioner_scan
from gateway.scheduler import start_scheduler, stop_scheduler
from shared.lib.db import ensure_runtime_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    logger.info("Gateway starting up")
    await ensure_runtime_schema()
    # Run initial provisioner scan (discover agent.yaml files)
    await run_provisioner_scan()
    # Start APScheduler (reads cron from agent.yaml)
    await start_scheduler()
    logger.info("Gateway ready")
    yield
    # Shutdown
    await stop_scheduler()
    logger.info("Gateway shut down")


app = FastAPI(
    title="Agentic Ops — Gateway",
    description="Web-facing gateway: webhooks, event collector, control-plane API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for control-plane UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://control-plane-ui:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount route modules
app.include_router(message_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(event_router, tags=["events"])
app.include_router(api_router, prefix="/api", tags=["control-plane"])


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "gateway"}
