"""Layer 1 — control-plane API endpoints."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.service


async def _make_client(fixture_workflows_dir: Path) -> httpx.AsyncClient:
    from shared.lib.config import settings

    settings.workflow_repo_paths = str(fixture_workflows_dir)
    from gateway.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.mark.asyncio
async def test_tasks_endpoint_lists_seeded_task(async_engine, fixture_workflows_dir: Path, db_session) -> None:
    from shared.lib.task_queue import create_task

    await create_task(db_session, workflow="platform-test", prompt="seeded")

    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        # API returns a list or paginated dict; just assert our prompt appears
        payload = resp.json()
        as_text = str(payload)
        assert "seeded" in as_text
        assert "platform-test" in as_text


@pytest.mark.asyncio
async def test_schedules_endpoint_returns_schedule_after_provision(async_engine, fixture_workflows_dir: Path) -> None:
    # Gateway lifespan runs provisioner on startup, but httpx ASGITransport
    # does not drive lifespan events — invoke provisioner explicitly.
    from unittest.mock import patch

    from gateway.provisioner import run_provisioner_scan
    from shared.lib.config import settings

    with patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)):
        await run_provisioner_scan()

    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        as_text = str(resp.json())
        assert "test-daily" in as_text


@pytest.mark.asyncio
async def test_platform_approvals_endpoint_reports_empty_counts(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/approvals")
        assert resp.status_code == 200
        payload = resp.json()
        assert "counts_by_status" in payload
        assert "items" in payload
        assert payload["items"] == []
