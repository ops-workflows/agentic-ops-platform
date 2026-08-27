"""Knowledge Source indexer worker entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.background_jobs import queue_background_job, reconcile_stale_background_jobs
from shared.lib.config import settings
from shared.lib.db import async_session_factory, engine, ensure_runtime_schema
from shared.lib.knowledge_source_indexer import (
    KNOWLEDGE_SOURCE_SYNC_JOB_TYPE,
    KnowledgeSourceBusyError,
    KnowledgeSourceIndexerConfig,
    KnowledgeSourceSyncError,
    run_knowledge_source_sync,
)
from shared.lib.models import BackgroundJobRun, KnowledgeSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = asyncio.Event()


def source_is_due(
    sync_policy: Any,
    *,
    last_started_at: datetime | None,
    now: datetime,
) -> bool:
    """Return whether an explicitly scheduled source is due."""
    if not isinstance(sync_policy, dict):
        return False
    interval_sec = sync_policy.get("interval_sec")
    if isinstance(interval_sec, bool) or not isinstance(interval_sec, int) or interval_sec <= 0:
        return False
    return last_started_at is None or last_started_at <= now - timedelta(seconds=interval_sec)


async def due_source_ids(session: AsyncSession, *, now: datetime | None = None) -> list[uuid.UUID]:
    """List enabled sources whose explicit interval has elapsed."""
    current_time = now or datetime.now(UTC)
    sources = list(
        (
            await session.execute(
                select(KnowledgeSource).where(KnowledgeSource.enabled.is_(True)).order_by(KnowledgeSource.id)
            )
        ).scalars()
    )
    due: list[uuid.UUID] = []
    for source in sources:
        last_started_at = await session.scalar(
            select(BackgroundJobRun.started_at)
            .where(
                BackgroundJobRun.job_type == KNOWLEDGE_SOURCE_SYNC_JOB_TYPE,
                BackgroundJobRun.knowledge_source_id == source.id,
            )
            .order_by(BackgroundJobRun.started_at.desc(), BackgroundJobRun.id.desc())
            .limit(1)
        )
        if source_is_due(source.sync_policy, last_started_at=last_started_at, now=current_time):
            due.append(source.id)
    return due


async def queued_sync_requests(
    session: AsyncSession,
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    rows = (
        await session.execute(
            select(
                BackgroundJobRun.id,
                BackgroundJobRun.knowledge_source_id,
                BackgroundJobRun.trigger,
            )
            .where(
                BackgroundJobRun.job_type == KNOWLEDGE_SOURCE_SYNC_JOB_TYPE,
                BackgroundJobRun.status == "queued",
                BackgroundJobRun.knowledge_source_id.is_not(None),
                BackgroundJobRun.trigger.in_(("scheduled", "manual", "retry")),
            )
            .order_by(BackgroundJobRun.started_at, BackgroundJobRun.id)
            .limit(100)
        )
    ).all()
    return [(run_id, source_id, trigger) for run_id, source_id, trigger in rows]


async def queue_due_sources(session: AsyncSession) -> None:
    for source_id in await due_source_ids(session):
        source = await session.get(KnowledgeSource, source_id)
        if source is not None:
            await queue_background_job(
                session,
                job_type=KNOWLEDGE_SOURCE_SYNC_JOB_TYPE,
                scope=source.canonical_alias,
                trigger="scheduled",
                knowledge_source_id=source.id,
                summary={"phase": "queued"},
            )


async def reconcile_worker_startup() -> list[uuid.UUID]:
    stale_after = int(settings.knowledge_source_stale_run_sec)
    if stale_after <= 0:
        raise ValueError("KNOWLEDGE_SOURCE_STALE_RUN_SEC must be positive")
    async with async_session_factory() as session:
        return await reconcile_stale_background_jobs(
            session,
            stale_before=datetime.now(UTC) - timedelta(seconds=stale_after),
            job_type=KNOWLEDGE_SOURCE_SYNC_JOB_TYPE,
        )


async def worker_loop() -> None:
    await ensure_runtime_schema()
    config = KnowledgeSourceIndexerConfig.from_settings()
    config.validate()
    poll_interval = int(settings.knowledge_source_indexer_poll_interval_sec)
    if poll_interval <= 0:
        raise ValueError("KNOWLEDGE_SOURCE_INDEXER_POLL_INTERVAL_SEC must be positive")
    reconciled = await reconcile_worker_startup()
    if reconciled:
        logger.warning("Reconciled %d stale Knowledge Source run(s): %s", len(reconciled), reconciled)
    logger.info("Knowledge Source indexer started with cache %s", config.cache_root)

    while not _shutdown.is_set():
        async with async_session_factory() as session:
            await queue_due_sources(session)
        async with async_session_factory() as session:
            requests = await queued_sync_requests(session)
        for run_id, source_id, trigger in requests:
            if _shutdown.is_set():
                break
            try:
                result = await run_knowledge_source_sync(
                    engine,
                    source_id=source_id,
                    trigger=trigger,
                    queued_run_id=run_id,
                    config=config,
                )
                logger.info(
                    "Knowledge Source sync %s: source=%s commit=%s run=%s",
                    result.status,
                    result.source_id,
                    result.commit_sha,
                    result.run_id,
                )
            except KnowledgeSourceBusyError:
                logger.info("Knowledge Source %s is already synchronizing", source_id)
            except KnowledgeSourceSyncError:
                logger.exception("Knowledge Source %s synchronization failed", source_id)

        with suppress(TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=poll_interval)


def _request_shutdown() -> None:
    _shutdown.set()


async def main() -> None:
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_name, _request_shutdown)
    await worker_loop()


if __name__ == "__main__":
    asyncio.run(main())
