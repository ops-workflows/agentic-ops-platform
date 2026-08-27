"""Shared lifecycle operations for non-agentic background jobs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.models import BackgroundJobRun

RUNNING_STATUS = "running"
QUEUED_STATUS = "queued"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "skipped", "cancelled"})


class BackgroundJobClaimError(RuntimeError):
    """Raised when a durable queued job was already claimed."""


async def queue_background_job(
    session: AsyncSession,
    *,
    job_type: str,
    scope: str | None = None,
    trigger: str | None = None,
    knowledge_source_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
    queued_at: datetime | None = None,
) -> BackgroundJobRun:
    """Persist durable work for a dedicated background worker."""
    run = BackgroundJobRun(
        job_type=job_type,
        scope=scope,
        trigger=trigger,
        knowledge_source_id=knowledge_source_id,
        status=QUEUED_STATUS,
        started_at=queued_at or datetime.now(UTC),
        summary=summary or {},
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def claim_background_job(session: AsyncSession, run_id: uuid.UUID) -> BackgroundJobRun:
    """Atomically transition one queued request to a running lifecycle."""
    run = await session.scalar(select(BackgroundJobRun).where(BackgroundJobRun.id == run_id).with_for_update())
    if run is None:
        raise BackgroundJobClaimError(f"Background job {run_id} does not exist")
    if run.status != QUEUED_STATUS:
        raise BackgroundJobClaimError(f"Background job {run_id} is already {run.status}")
    started_at = datetime.now(UTC)
    run.status = RUNNING_STATUS
    run.started_at = started_at
    run.heartbeat_at = started_at
    await session.commit()
    await session.refresh(run)
    return run


async def start_background_job(
    session: AsyncSession,
    *,
    job_type: str,
    scope: str | None = None,
    trigger: str | None = None,
    knowledge_source_id: uuid.UUID | None = None,
    knowledge_source_version_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
    started_at: datetime | None = None,
) -> BackgroundJobRun:
    """Persist a running job before its external work begins."""
    now = started_at or datetime.now(UTC)
    run = BackgroundJobRun(
        job_type=job_type,
        scope=scope,
        trigger=trigger,
        knowledge_source_id=knowledge_source_id,
        knowledge_source_version_id=knowledge_source_version_id,
        status=RUNNING_STATUS,
        started_at=now,
        heartbeat_at=now,
        summary=summary or {},
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def heartbeat_background_job(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    summary: dict[str, Any] | None = None,
) -> None:
    """Refresh liveness and optionally replace the job's progress summary."""
    values: dict[str, Any] = {"heartbeat_at": datetime.now(UTC)}
    if summary is not None:
        values["summary"] = summary
    result = await session.execute(
        update(BackgroundJobRun)
        .where(BackgroundJobRun.id == run_id, BackgroundJobRun.status == RUNNING_STATUS)
        .values(**values)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(f"Background job {run_id} is not running")
    await session.commit()


async def finish_background_job(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
    knowledge_source_version_id: uuid.UUID | None = None,
    finished_at: datetime | None = None,
) -> BackgroundJobRun:
    """Finish one running job while preserving its original start time."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Unsupported background job terminal status: {status}")
    result = await session.execute(select(BackgroundJobRun).where(BackgroundJobRun.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        raise ValueError(f"Background job {run_id} does not exist")
    if run.status != RUNNING_STATUS:
        raise ValueError(f"Background job {run_id} is already {run.status}")

    completed_at = finished_at or datetime.now(UTC)
    run.status = status
    run.heartbeat_at = completed_at
    run.finished_at = completed_at
    run.duration_sec = max(0.0, (completed_at - run.started_at).total_seconds())
    if summary is not None:
        run.summary = summary
    if warnings is not None:
        run.warnings = warnings
    run.error = error
    if knowledge_source_version_id is not None:
        run.knowledge_source_version_id = knowledge_source_version_id
    await session.commit()
    await session.refresh(run)
    return run


async def record_completed_background_job(
    session: AsyncSession,
    *,
    job_type: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    scope: str | None = None,
    trigger: str | None = None,
    knowledge_source_id: uuid.UUID | None = None,
    knowledge_source_version_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
) -> BackgroundJobRun:
    """Record work whose lifecycle was managed by an existing loop."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Unsupported background job terminal status: {status}")
    run = BackgroundJobRun(
        job_type=job_type,
        scope=scope,
        trigger=trigger,
        knowledge_source_id=knowledge_source_id,
        knowledge_source_version_id=knowledge_source_version_id,
        status=status,
        started_at=started_at,
        heartbeat_at=finished_at,
        finished_at=finished_at,
        duration_sec=max(0.0, (finished_at - started_at).total_seconds()),
        summary=summary or {},
        warnings=warnings or [],
        error=error,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def reconcile_stale_background_jobs(
    session: AsyncSession,
    *,
    stale_before: datetime,
    job_type: str | None = None,
) -> list[uuid.UUID]:
    """Mark running jobs without a recent heartbeat as failed."""
    query = select(BackgroundJobRun).where(
        BackgroundJobRun.status == RUNNING_STATUS,
        or_(
            BackgroundJobRun.heartbeat_at < stale_before,
            BackgroundJobRun.heartbeat_at.is_(None) & (BackgroundJobRun.started_at < stale_before),
        ),
    )
    if job_type is not None:
        query = query.where(BackgroundJobRun.job_type == job_type)
    result = await session.execute(query.with_for_update())
    runs = list(result.scalars())
    reconciled_at = datetime.now(UTC)
    for run in runs:
        run.status = "failed"
        run.heartbeat_at = reconciled_at
        run.finished_at = reconciled_at
        run.duration_sec = max(0.0, (reconciled_at - run.started_at).total_seconds())
        run.summary = {**(run.summary or {}), "failure_phase": "startup_reconciliation"}
        run.error = "Background job heartbeat expired before worker startup"
    await session.commit()
    return [run.id for run in runs]
