"""Layer 1 — /platform/* catalog endpoints.

Exercises the hindsight-, connector-, and agent-memory catalogs at the
HTTP layer. Hindsight is not reachable in the test environment, so the
endpoint should return ``hindsight_available: false`` and an empty bank
list (not 500).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.service


async def _make_client(fixture_workflows_dir: Path) -> httpx.AsyncClient:
    from shared.lib.config import settings

    settings.workflow_repo_paths = str(fixture_workflows_dir)
    # Point hindsight at an unreachable host so _hindsight_available() is False
    settings.hindsight_url = "http://127.0.0.1:1"
    # Object store client construction requires a non-empty secret key
    if not getattr(settings, "object_store_secret_key", ""):
        settings.object_store_secret_key = "test-secret"

    from gateway.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.mark.asyncio
async def test_platform_memories_endpoint_returns_shape(async_engine, fixture_workflows_dir: Path, monkeypatch) -> None:
    # The object store is not reachable from the test host — stub the catalog
    # list call so the endpoint returns an empty agent_memories list rather
    # than 500.
    from gateway import api as gateway_api

    monkeypatch.setattr(gateway_api, "list_objects", lambda *a, **k: [])  # noqa: ARG005

    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/memories")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # Shape assertions
        assert "hindsight_available" in payload
        assert "hindsight_banks" in payload
        assert "agent_memories" in payload
        # Hindsight unreachable in tests
        assert payload["hindsight_available"] is False
        assert isinstance(payload["hindsight_banks"], list)


@pytest.mark.asyncio
async def test_platform_connectors_endpoint_lists_configured_instances(
    async_engine, fixture_workflows_dir: Path, monkeypatch, tmp_path
) -> None:
    from shared.lib.config import settings

    config = tmp_path / "platform-config.yaml"
    config.write_text(
        "connectors:\n"
        "  enabled:\n"
        "    - sf-intake\n"
        "  instances:\n"
        "    sf-intake:\n"
        "      type: gcp-pubsub\n"
        "      display_name: SF Intake\n"
        "      source:\n"
        "        type: pubsub\n"
        "        subscription: my-sub\n"
        "      target:\n"
        "        workflow: sf-alerts-investigator\n"
        "        message_channel: sf-alerts\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "platform_config_file", str(config))

    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/connectors")
        assert resp.status_code == 200, resp.text
        connectors = resp.json()
        ids = {c["id"] for c in connectors}
        assert "sf-intake" in ids


@pytest.mark.asyncio
async def test_platform_mcp_endpoint_returns_catalog(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/api/platform/mcp")
        assert resp.status_code == 200, resp.text
        servers = resp.json()
        assert isinstance(servers, list)
        # The test fixture plugin declares a `testserver` MCP, but whether
        # it shows up here depends on mcps catalog — the point is
        # that the endpoint returns 200 and a list.
