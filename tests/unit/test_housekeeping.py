from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from shared.lib.housekeeping import (
    _parse_hindsight_timestamp,
    learning_bank_ids,
    prune_agent_memory_versions,
    prune_learning_bank_memories,
)
from shared.lib.memory_catalog import BANK_WORKFLOW_LEARNING

pytestmark = pytest.mark.unit


def test_learning_bank_ids_include_default_learning_bank_only_once():
    bank_ids = learning_bank_ids()
    assert BANK_WORKFLOW_LEARNING in bank_ids
    assert len(bank_ids) == len(set(bank_ids))


def test_parse_hindsight_timestamp_normalizes_zulu_time():
    parsed = _parse_hindsight_timestamp("2025-01-02T03:04:05Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).isoformat() == "2025-01-02T03:04:05+00:00"


def test_parse_hindsight_timestamp_returns_none_for_bad_values():
    assert _parse_hindsight_timestamp("not-a-date") is None
    assert _parse_hindsight_timestamp(None) is None


def test_prune_agent_memory_versions_keeps_latest_and_recent_versions(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=UTC)
    deleted: list[str] = []

    @dataclass
    class Obj:
        key: str
        size: int
        last_modified: datetime

    objects = [
        Obj("agent/latest.tar.gz", 1, now),
        Obj("agent/20250110T000000.tar.gz", 1, now),
        Obj("agent/20250109T000000.tar.gz", 1, now - timedelta(days=1)),
        Obj("agent/20250108T000000.tar.gz", 1, now - timedelta(days=2)),
    ]

    monkeypatch.setattr("shared.lib.housekeeping.list_objects", lambda bucket: objects)
    monkeypatch.setattr("shared.lib.housekeeping.delete_object", lambda bucket, key: deleted.append(key) or True)

    count = prune_agent_memory_versions(versions_to_keep=2, retention_days=0, now=now)
    assert count == 1
    assert deleted == ["agent/20250108T000000.tar.gz"]


@pytest.mark.asyncio
async def test_prune_learning_bank_memories_uses_supported_hindsight_endpoints(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=UTC)
    requests: list[tuple[str, str, str | None]] = []
    original_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode() if request.content else None
        requests.append((request.method, request.url.path, body))

        if request.method == "GET" and request.url.path == "/v1/default/banks/learning-bank/memories/list":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "mem-doc-1",
                            "document_id": "doc-1",
                            "created_at": "2025-01-01T00:00:00Z",
                        },
                        {
                            "id": "mem-doc-2",
                            "document_id": "doc-1",
                            "created_at": "2025-01-01T00:00:00Z",
                        },
                        {
                            "id": "mem-standalone",
                            "created_at": "2025-01-01T00:00:00Z",
                        },
                    ]
                },
            )

        if request.method == "DELETE" and request.url.path == "/v1/default/banks/learning-bank/documents/doc-1":
            return httpx.Response(200, json={"memory_units_deleted": 2})

        if request.method == "PATCH" and request.url.path == "/v1/default/banks/learning-bank/memories/mem-standalone":
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404, json={"detail": "unexpected request"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("shared.lib.housekeeping.settings.hindsight_url", "http://hindsight.test")
    monkeypatch.setattr(
        "shared.lib.housekeeping.httpx.AsyncClient",
        lambda timeout=10.0: original_async_client(transport=transport, timeout=timeout),
    )

    count, warnings = await prune_learning_bank_memories(
        retention_days=5,
        bank_ids=["learning-bank"],
        now=now,
    )

    assert count == 3
    assert warnings == []
    assert requests == [
        ("GET", "/v1/default/banks/learning-bank/memories/list", None),
        ("DELETE", "/v1/default/banks/learning-bank/documents/doc-1", None),
        (
            "PATCH",
            "/v1/default/banks/learning-bank/memories/mem-standalone",
            '{"state":"invalidated","reason":"housekeeping retention: older than 5 days"}',
        ),
    ]


@pytest.mark.asyncio
async def test_prune_learning_bank_memories_retries_transient_transport_failures(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=UTC)
    attempts = 0
    original_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "GET" and request.url.path == "/v1/default/banks/learning-bank/memories/list":
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("not ready", request=request)
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"detail": "unexpected request"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("shared.lib.housekeeping.settings.hindsight_url", "http://hindsight.test")
    monkeypatch.setattr("shared.lib.housekeeping.settings.hindsight_request_retries", 3)
    monkeypatch.setattr("shared.lib.housekeeping.settings.hindsight_request_retry_backoff_sec", 0.0)
    monkeypatch.setattr(
        "shared.lib.housekeeping.httpx.AsyncClient",
        lambda timeout=10.0: original_async_client(transport=transport, timeout=timeout),
    )

    count, warnings = await prune_learning_bank_memories(
        retention_days=5,
        bank_ids=["learning-bank"],
        now=now,
    )

    assert count == 0
    assert warnings == []
    assert attempts == 3


@pytest.mark.asyncio
async def test_prune_learning_bank_memories_skips_observations_without_documents(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=UTC)
    requests: list[tuple[str, str]] = []
    original_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/default/banks/learning-bank/memories/list":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "observation-1",
                            "fact_type": "observation",
                            "created_at": "2025-01-01T00:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected request"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("shared.lib.housekeeping.settings.hindsight_url", "http://hindsight.test")
    monkeypatch.setattr(
        "shared.lib.housekeeping.httpx.AsyncClient",
        lambda timeout=10.0: original_async_client(transport=transport, timeout=timeout),
    )

    count, warnings = await prune_learning_bank_memories(
        retention_days=5,
        bank_ids=["learning-bank"],
        now=now,
    )

    assert count == 0
    assert warnings == []
    assert requests == [("GET", "/v1/default/banks/learning-bank/memories/list")]
