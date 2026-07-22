"""Layer 1 — /platform/workflow-repo/* endpoints (status, sync, pin, versions)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from shared.lib.models import Agent, WorkflowRepoState

pytestmark = pytest.mark.service


async def _make_client(fixture_workflows_dir: Path) -> httpx.AsyncClient:
    from shared.lib.config import settings

    settings.workflow_repo_paths = str(fixture_workflows_dir)
    settings.workflow_repo_url = ""
    settings.hindsight_url = "http://127.0.0.1:1"
    if not getattr(settings, "object_store_secret_key", ""):
        settings.object_store_secret_key = "test-secret"

    from gateway.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.fixture(autouse=True)
async def _reset_workflow_repo_state(async_engine, db_session):  # noqa: ARG001
    await db_session.execute(delete(WorkflowRepoState))
    await db_session.commit()
    yield
    await db_session.execute(delete(WorkflowRepoState))
    await db_session.commit()


@pytest.mark.asyncio
async def test_workflow_repo_status_returns_local_source_mode_by_default(
    async_engine, fixture_workflows_dir: Path
) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/workflow-repo")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["source_mode"] == "local"
        assert payload["source_url"] is None
        assert payload["pinned_ref"] is None
        assert payload["discovered_workflows"] == []


@pytest.mark.asyncio
async def test_workflow_repo_sync_discovers_and_persists_state(
    async_engine, db_session, fixture_workflows_dir: Path, monkeypatch
) -> None:
    from shared.lib.config import settings

    monkeypatch.setattr(settings, "runtime_bundle_root", "")

    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.post("/api/platform/workflow-repo/sync")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["last_sync_status"] == "ok"
        assert "platform-test" in payload["discovered_workflows"]

        status_resp = await client.get("/api/platform/workflow-repo")
        assert status_resp.json()["last_sync_status"] == "ok"

    result = await db_session.execute(select(Agent).where(Agent.name == "platform-test"))
    agent = result.scalar_one()
    assert agent.provisioned is True
    assert agent.config["runtime"]["parallel_workers"] == 1


@pytest.mark.asyncio
async def test_workflow_repo_pin_persists_and_rejects_blank_ref(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.post("/api/platform/workflow-repo/pin", json={"ref": "v2.0.0"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["pinned_ref"] == "v2.0.0"

        bad_resp = await client.post("/api/platform/workflow-repo/pin", json={"ref": "   "})
        assert bad_resp.status_code == 400


@pytest.mark.asyncio
async def test_workflow_repo_versions_returns_empty_for_non_github_or_missing_url(
    async_engine, fixture_workflows_dir: Path
) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/workflow-repo/versions")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []
