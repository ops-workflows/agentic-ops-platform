"""Layer 1 — gateway HTTP APIs via ASGI transport.

Uses httpx.AsyncClient with ASGI transport to hit the gateway FastAPI app
in-process without spawning a network server.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

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
async def test_message_reply_endpoint_returns_websocket_ingested_reply(
    async_engine, fixture_workflows_dir: Path, db_session
) -> None:
    from shared.lib.models import SessionEvent, Task

    task = Task(
        id=uuid.uuid4(),
        workflow="platform-test",
        prompt="Need an answer",
        status="waiting_user_input",
        message_channel="platform-test-channel",
        message_thread="thread-xyz",
    )
    db_session.add(task)
    await db_session.commit()
    db_session.add(
        SessionEvent(
            task_id=task.id,
            event_type="user_question_reply",
            timestamp=datetime.now(UTC),
            data={"message": "the answer", "user_id": "user-1", "username": "alice"},
        )
    )
    await db_session.commit()

    async with await _make_client(fixture_workflows_dir) as client:
        response = await client.get(f"/api/tasks/{task.id}/message-reply", params={"after_ms": 0})

    assert response.status_code == 200
    assert response.json() == {"message": "the answer", "user_id": "user-1", "username": "alice"}


@pytest.mark.asyncio
async def test_session_detail_endpoint_returns_parsed_trace(
    async_engine, fixture_workflows_dir: Path, db_session
) -> None:
    from shared.lib.models import SessionEvent, Task

    task = Task(
        id=uuid.uuid4(),
        workflow="platform-test",
        prompt="Investigate alert",
        status="succeeded",
    )
    db_session.add(task)
    await db_session.commit()
    db_session.add(
        SessionEvent(
            task_id=task.id,
            event_type="session_start",
            timestamp=datetime.now(UTC),
            data={"agent": "test-coordinator", "prompt_preview": "Investigate alert"},
        )
    )
    db_session.add(
        SessionEvent(
            task_id=task.id,
            event_type="session_complete",
            timestamp=datetime.now(UTC),
            data={"result": "All clear", "input_tokens": 100, "output_tokens": 50},
        )
    )
    await db_session.commit()

    async with await _make_client(fixture_workflows_dir) as client:
        response = await client.get(f"/api/sessions/{task.id}")

    assert response.status_code == 200
    payload = response.json()
    assert "trace" in payload
    assert payload["trace"]["root"]["label"] == "test-coordinator"
    assert len(payload["trace"]["root"]["children"]) == 2
    assert payload["trace"]["root"]["children"][0]["badge"] == "REQUEST"
    assert payload["trace"]["root"]["children"][1]["label"] == "Final"
    assert payload["trace"]["root"]["children"][1]["body"] == "All clear"
    assert payload["trace"]["stats"]["tokensIn"] == 100
    assert payload["trace"]["stats"]["tokensOut"] == 50
    assert "events" not in payload


@pytest.mark.asyncio
async def test_unknown_route_returns_404(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.get("/definitely-not-a-route")
        assert resp.status_code == 404
