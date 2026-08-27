"""Shared non-agentic background-job lifecycle service tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.lib.background_jobs import (
    claim_background_job,
    finish_background_job,
    heartbeat_background_job,
    queue_background_job,
    reconcile_stale_background_jobs,
    start_background_job,
)
from shared.lib.models import BackgroundJobRun

pytestmark = pytest.mark.asyncio


async def test_background_job_lifecycle_persists_progress_and_terminal_state(db_session) -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=3)
    run = await start_background_job(
        db_session,
        job_type="knowledge_source_sync",
        scope="org/application",
        trigger="manual",
        summary={"phase": "resolve_ref"},
        started_at=started_at,
    )

    await heartbeat_background_job(db_session, run.id, summary={"phase": "extract"})
    completed = await finish_background_job(
        db_session,
        run.id,
        status="succeeded",
        summary={"phase": "complete"},
    )

    assert completed.status == "succeeded"
    assert completed.summary == {"phase": "complete"}
    assert completed.heartbeat_at == completed.finished_at
    assert completed.duration_sec is not None and completed.duration_sec >= 3


async def test_startup_reconciliation_fails_only_stale_running_jobs(db_session) -> None:
    now = datetime.now(UTC)
    stale = BackgroundJobRun(
        job_type="knowledge_source_sync",
        status="running",
        started_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
        summary={"phase": "extract"},
    )
    current = BackgroundJobRun(
        job_type="knowledge_source_sync",
        status="running",
        started_at=now,
        heartbeat_at=now,
    )
    db_session.add_all([stale, current])
    await db_session.commit()

    reconciled = await reconcile_stale_background_jobs(
        db_session,
        stale_before=now - timedelta(hours=1),
        job_type="knowledge_source_sync",
    )

    await db_session.refresh(stale)
    await db_session.refresh(current)
    assert reconciled == [stale.id]
    assert stale.status == "failed"
    assert stale.summary["failure_phase"] == "startup_reconciliation"
    assert current.status == "running"


async def test_queued_job_is_durably_claimed_before_work(db_session) -> None:
    queued_at = datetime.now(UTC) - timedelta(seconds=5)
    queued = await queue_background_job(
        db_session,
        job_type="knowledge_source_sync",
        trigger="manual",
        queued_at=queued_at,
    )

    claimed = await claim_background_job(db_session, queued.id)

    assert claimed.status == "running"
    assert claimed.started_at > queued_at
    assert claimed.heartbeat_at == claimed.started_at
