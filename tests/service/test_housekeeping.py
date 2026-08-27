from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from shared.lib.housekeeping import (
    prune_background_job_runs,
    prune_knowledge_source_history,
    record_background_job_run,
)
from shared.lib.models import BackgroundJobRun, KnowledgeSource, KnowledgeSourceVersion

pytestmark = pytest.mark.asyncio


async def test_prune_background_job_runs_keeps_latest_rows(db_session):
    anchor = datetime(2025, 1, 10, tzinfo=UTC)
    for index in range(7):
        db_session.add(
            BackgroundJobRun(
                job_type="housekeeping",
                scope="platform",
                status="succeeded",
                started_at=anchor + timedelta(minutes=index),
                finished_at=anchor + timedelta(minutes=index, seconds=1),
                duration_sec=1.0,
                summary={"run": index},
                warnings=[],
            )
        )
    await db_session.commit()

    pruned = await prune_background_job_runs(db_session, keep_latest=4)

    assert pruned == 3

    rows = (
        (
            await db_session.execute(
                select(BackgroundJobRun).order_by(BackgroundJobRun.started_at.desc(), BackgroundJobRun.id.desc())
            )
        )
        .scalars()
        .all()
    )
    assert [row.summary["run"] for row in rows] == [6, 5, 4, 3]


async def test_record_background_job_run_persists_duration(db_session):
    started_at = datetime(2025, 1, 10, tzinfo=UTC)
    run = await record_background_job_run(
        db_session,
        job_type="housekeeping",
        scope="platform",
        status="succeeded",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
    )

    assert run.duration_sec == 2.0


async def test_prune_knowledge_source_history_keeps_latest_versions_and_runs(db_session, monkeypatch):
    anchor = datetime(2025, 1, 10, tzinfo=UTC)
    source = KnowledgeSource(
        canonical_alias="application",
        repository_url="https://github.example.com/org/application.git",
        default_ref="main",
    )
    db_session.add(source)
    await db_session.flush()
    versions = []
    runs = []
    for index in range(7):
        version = KnowledgeSourceVersion(
            source_id=source.id,
            commit_sha=f"{index:040x}",
            status="succeeded",
            graphify_version="0.9.40",
            extraction_config_hash=f"{index:064x}",
            artifact_keys={"graph": f"knowledge-sources/application/{index}/graph.json"},
            created_at=anchor + timedelta(minutes=index),
        )
        db_session.add(version)
        await db_session.flush()
        versions.append(version)
        runs.append(
            BackgroundJobRun(
                job_type="knowledge_source_sync",
                knowledge_source_id=source.id,
                knowledge_source_version_id=version.id,
                status="succeeded",
                started_at=anchor + timedelta(minutes=index),
                summary={"run": index},
                warnings=[],
            )
        )
    source.current_successful_version_id = versions[-1].id
    db_session.add_all(runs)
    await db_session.commit()

    deleted: list[str] = []

    class Store:
        def delete_object(self, bucket, key):
            assert bucket == "knowledge"
            deleted.append(key)
            return True

    monkeypatch.setattr("shared.lib.housekeeping.settings.knowledge_source_object_store_bucket", "knowledge")
    versions_pruned, runs_pruned, warnings = await prune_knowledge_source_history(
        db_session,
        object_store=Store(),
    )

    assert (versions_pruned, runs_pruned, warnings) == (2, 2, [])
    assert sorted(deleted) == [
        "knowledge-sources/application/0/graph.json",
        "knowledge-sources/application/1/graph.json",
    ]
    remaining_versions = list(
        (await db_session.execute(select(KnowledgeSourceVersion).order_by(KnowledgeSourceVersion.created_at))).scalars()
    )
    remaining_runs = list(
        (await db_session.execute(select(BackgroundJobRun).order_by(BackgroundJobRun.started_at))).scalars()
    )
    assert [version.commit_sha for version in remaining_versions] == [f"{index:040x}" for index in range(2, 7)]
    assert [run.summary["run"] for run in remaining_runs] == [2, 3, 4, 5, 6]
