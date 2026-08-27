from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.lib.knowledge_source_publication import (
    KnowledgeSourcePublication,
    fail_knowledge_source_version,
    promote_knowledge_source_version,
)
from shared.lib.models import (
    BackgroundJobRun,
    KnowledgeSource,
    KnowledgeSourceVersion,
)

pytestmark = pytest.mark.asyncio


async def test_persist_source_version_and_linked_run(db_session):
    source = KnowledgeSource(
        canonical_alias="onlinefirst/online",
        repository_url="https://github.example.com/onlinefirst/online.git",
        default_ref="main",
        include_paths=["src/**"],
    )
    db_session.add(source)
    await db_session.flush()

    version = KnowledgeSourceVersion(
        source_id=source.id,
        commit_sha="a" * 40,
        graphify_version="0.1.0",
        extraction_config_hash="b" * 64,
        status="succeeded",
        artifact_keys={
            "manifest": f"knowledge-sources/{source.id}/{'a' * 40}/manifest.json",
            "graph": f"knowledge-sources/{source.id}/{'a' * 40}/graph.json",
            "source": f"knowledge-sources/{source.id}/{'a' * 40}/source.tar.zst",
        },
        artifact_checksums={"graph": "sha256:graph", "source": "sha256:source"},
        file_count=12,
        node_count=48,
        edge_count=72,
    )
    db_session.add(version)
    await db_session.flush()

    source.current_successful_version_id = version.id
    run = BackgroundJobRun(
        job_type="knowledge_source_sync",
        scope=source.canonical_alias,
        trigger="manual",
        knowledge_source_id=source.id,
        knowledge_source_version_id=version.id,
        status="succeeded",
        summary={"commit_sha": version.commit_sha},
    )
    db_session.add(run)
    await db_session.commit()

    persisted_source = await db_session.get(KnowledgeSource, source.id)
    persisted_run = await db_session.get(BackgroundJobRun, run.id)

    assert persisted_source is not None
    assert persisted_source.current_successful_version_id == version.id
    assert persisted_run is not None
    assert persisted_run.knowledge_source_id == source.id
    assert persisted_run.knowledge_source_version_id == version.id


async def test_successful_publication_atomically_promotes_version_and_run(db_session):
    started_at = datetime.now(UTC) - timedelta(seconds=5)
    source = KnowledgeSource(
        canonical_alias="org/application",
        repository_url="https://github.example.com/org/application.git",
        default_ref="main",
    )
    db_session.add(source)
    await db_session.flush()
    version = KnowledgeSourceVersion(
        source_id=source.id,
        commit_sha="c" * 40,
        status="running",
        graphify_version="0.9.40",
        extraction_config_hash="d" * 64,
        started_at=started_at,
    )
    db_session.add(version)
    await db_session.flush()
    run = BackgroundJobRun(
        job_type="knowledge_source_sync",
        scope=source.canonical_alias,
        trigger="manual",
        knowledge_source_id=source.id,
        knowledge_source_version_id=version.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
    )
    db_session.add(run)
    await db_session.commit()
    publication = KnowledgeSourcePublication(
        bucket="knowledge",
        artifact_keys={"graph": "knowledge-sources/source/commit/graph.json"},
        artifact_checksums={"graph": "sha256:abc"},
    )

    await promote_knowledge_source_version(
        db_session,
        source_id=source.id,
        version_id=version.id,
        run_id=run.id,
        publication=publication,
        file_count=3,
        node_count=8,
        edge_count=13,
        phase_timings={"extract": 1.25, "publish": 0.5},
    )

    await db_session.refresh(source)
    await db_session.refresh(run)
    assert source.current_successful_version_id == version.id
    assert version.status == "succeeded"
    assert version.artifact_keys == publication.artifact_keys
    assert run.status == "succeeded"
    assert run.summary["node_count"] == 8
    assert run.finished_at is not None


async def test_failed_version_preserves_previous_successful_pointer(db_session):
    started_at = datetime.now(UTC) - timedelta(seconds=5)
    source = KnowledgeSource(
        canonical_alias="org/application",
        repository_url="https://github.example.com/org/application.git",
        default_ref="main",
    )
    db_session.add(source)
    await db_session.flush()
    previous = KnowledgeSourceVersion(
        source_id=source.id,
        commit_sha="a" * 40,
        status="succeeded",
        graphify_version="0.9.40",
        extraction_config_hash="b" * 64,
    )
    attempted = KnowledgeSourceVersion(
        source_id=source.id,
        commit_sha="c" * 40,
        status="running",
        graphify_version="0.9.40",
        extraction_config_hash="d" * 64,
        started_at=started_at,
    )
    db_session.add_all([previous, attempted])
    await db_session.flush()
    source.current_successful_version_id = previous.id
    run = BackgroundJobRun(
        job_type="knowledge_source_sync",
        scope=source.canonical_alias,
        trigger="scheduled",
        knowledge_source_id=source.id,
        knowledge_source_version_id=attempted.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
    )
    db_session.add(run)
    await db_session.commit()

    await fail_knowledge_source_version(
        db_session,
        source_id=source.id,
        version_id=attempted.id,
        run_id=run.id,
        failure_phase="publish",
        error="checksum mismatch",
        phase_timings={"extract": 1.25, "publish": 0.1},
    )

    await db_session.refresh(source)
    await db_session.refresh(run)
    assert source.current_successful_version_id == previous.id
    assert attempted.status == "failed"
    assert attempted.error == "checksum mismatch"
    assert run.status == "failed"
    assert run.summary["failure_phase"] == "publish"
