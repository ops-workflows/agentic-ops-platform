"""Knowledge Source control-plane API service contracts."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.api import (
    KnowledgeSourceWriteRequest,
    create_knowledge_source,
    get_platform_background_jobs,
    queue_knowledge_source_sync,
)
from shared.lib.platform_secrets import GitHubAppConnection

pytestmark = pytest.mark.asyncio


async def test_manual_sync_queues_same_typed_run_exposed_by_filters(async_engine, monkeypatch) -> None:
    monkeypatch.setattr(
        "gateway.api.load_github_app_connections",
        lambda _path: {
            "github-public": GitHubAppConnection(
                "github-public", "https://github.example.com", "https://api.github.example.com", "1", "2", ""
            )
        },
    )
    source = await create_knowledge_source(
        KnowledgeSourceWriteRequest(
            repository="org/application",
            credential_ref="github-public",
            include_paths=["src/**"],
            sync_policy={"interval_sec": 300},
        )
    )

    assert source.repository_url == "https://github.example.com/org/application.git"

    queued = await queue_knowledge_source_sync(source.id)
    filtered = await get_platform_background_jobs(
        limit=20,
        offset=0,
        job_type="knowledge_source_sync",
        status="queued",
        trigger="manual",
        knowledge_source_id=source.id,
    )

    assert queued.status == "queued"
    assert queued.knowledge_source_id == source.id
    assert filtered.total == 1
    assert filtered.items[0].id == queued.id
    with pytest.raises(HTTPException) as duplicate:
        await queue_knowledge_source_sync(source.id)
    assert duplicate.value.status_code == 409


async def test_source_write_rejects_unknown_connection_and_invalid_repository(async_engine, monkeypatch) -> None:
    monkeypatch.setattr("gateway.api.load_github_app_connections", lambda _path: {})
    with pytest.raises(HTTPException) as unknown_connection:
        await create_knowledge_source(
            KnowledgeSourceWriteRequest(
                repository="org/application",
                credential_ref="github-public",
            )
        )
    assert unknown_connection.value.status_code == 422

    monkeypatch.setattr(
        "gateway.api.load_github_app_connections",
        lambda _path: {
            "github-public": GitHubAppConnection(
                "github-public", "https://github.example.com", "https://api.github.example.com", "1", "2", ""
            )
        },
    )
    with pytest.raises(HTTPException) as repository:
        await create_knowledge_source(
            KnowledgeSourceWriteRequest(
                repository="token@github.example.com/org/application",
                credential_ref="github-public",
            )
        )
    assert repository.value.status_code == 422
