"""Layer 1 — gateway HTTP APIs via ASGI transport.

Uses httpx.AsyncClient with ASGI transport to hit the gateway FastAPI app
in-process without spawning a network server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.service


async def _make_client(fixture_workflows_dir: Path) -> httpx.AsyncClient:
    from shared.lib.config import settings

    # Point the gateway at the synthetic test plugin tree for provisioner +
    # message routing.
    settings.workflow_repo_paths = str(fixture_workflows_dir)

    from gateway.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.mark.asyncio
async def test_health_endpoint(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "gateway"


@pytest.mark.asyncio
async def test_message_webhook_help_shortcut(async_engine, fixture_workflows_dir: Path) -> None:
    from shared.lib.config import settings

    with patch.object(settings, "message_outgoing_webhook_secret", ""):
        async with await _make_client(fixture_workflows_dir) as client:
            resp = await client.post(
                "/webhooks/message",
                json={"text": "help", "channel_name": "platform-test-channel", "token": ""},
            )
            assert resp.status_code == 200
            assert resp.json()["response_type"] == "comment"


@pytest.mark.asyncio
async def test_message_webhook_invalid_token_returns_403(async_engine, fixture_workflows_dir: Path) -> None:
    from shared.lib.config import settings

    with patch.object(settings, "message_outgoing_webhook_secret", "expected"):
        async with await _make_client(fixture_workflows_dir) as client:
            resp = await client.post(
                "/webhooks/message",
                json={"text": "anything", "channel_name": "platform-test-channel", "token": "wrong"},
            )
            assert resp.status_code == 403


@pytest.mark.asyncio
async def test_message_webhook_creates_task_for_known_channel(
    async_engine, fixture_workflows_dir: Path, db_session
) -> None:
    from sqlalchemy import select

    from shared.lib.config import settings
    from shared.lib.models import Task

    with patch.object(settings, "message_outgoing_webhook_secret", ""):
        async with await _make_client(fixture_workflows_dir) as client:
            resp = await client.post(
                "/webhooks/message",
                json={
                    "text": "@agent investigate something",
                    "channel_name": "platform-test-channel",
                    "trigger_word": "@agent",
                    "token": "",
                    "user_name": "tester",
                    "post_id": "thread-xyz",
                    "channel_id": "chan-xyz",
                },
            )
            assert resp.status_code == 200

    tasks = (await db_session.execute(select(Task).where(Task.workflow == "platform-test"))).scalars().all()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.prompt == "investigate something"
    assert task.message_thread == "thread-xyz"
    assert task.message_channel == "platform-test-channel"


@pytest.mark.asyncio
async def test_message_webhook_unknown_channel_returns_help(async_engine, fixture_workflows_dir: Path) -> None:
    from shared.lib.config import settings

    with patch.object(settings, "message_outgoing_webhook_secret", ""):
        async with await _make_client(fixture_workflows_dir) as client:
            resp = await client.post(
                "/webhooks/message",
                json={
                    "text": "@agent do thing",
                    "channel_name": "not-a-real-channel",
                    "trigger_word": "@agent",
                    "token": "",
                },
            )
            assert resp.status_code == 200
            assert "Available workflow channels" in resp.json()["text"] or "No Message" in resp.json()["text"]


@pytest.mark.asyncio
async def test_unknown_route_returns_404(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/definitely-not-a-route")
        assert resp.status_code == 404
