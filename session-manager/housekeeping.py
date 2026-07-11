"""Background housekeeping loop for archives and retention."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.housekeeping import record_background_job_run, run_housekeeping_once

logger = logging.getLogger(__name__)


async def housekeeping_loop() -> None:
    if not settings.housekeeping_enabled:
        logger.info("Housekeeping disabled")
        return

    interval = max(60, int(settings.housekeeping_interval_sec or 3600))
    logger.info("Housekeeping loop started (every %ds)", interval)
    while True:
        started_at = datetime.now(UTC)
        try:
            report = await run_housekeeping_once()
            finished_at = datetime.now(UTC)
            async with async_session_factory() as session:
                await record_background_job_run(
                    session,
                    job_type="housekeeping",
                    scope="platform",
                    status="succeeded",
                    started_at=started_at,
                    finished_at=finished_at,
                    summary={
                        "archived_tasks": report.archived_tasks,
                        "deleted_tasks": report.deleted_tasks,
                        "pruned_agent_memory_versions": report.pruned_agent_memory_versions,
                        "pruned_learning_memories": report.pruned_learning_memories,
                        "pruned_background_job_runs": report.pruned_background_job_runs,
                    },
                    warnings=report.warnings,
                )
            if (
                report.archived_tasks
                or report.deleted_tasks
                or report.pruned_agent_memory_versions
                or report.pruned_learning_memories
                or report.pruned_background_job_runs
            ):
                logger.info(
                    (
                        "Housekeeping complete: archived=%d deleted=%d "
                        "pruned_memory=%d pruned_learning=%d "
                        "pruned_job_history=%d warnings=%d"
                    ),
                    report.archived_tasks,
                    report.deleted_tasks,
                    report.pruned_agent_memory_versions,
                    report.pruned_learning_memories,
                    report.pruned_background_job_runs,
                    len(report.warnings),
                )
        except Exception as exc:
            logger.exception("Housekeeping pass failed")
            finished_at = datetime.now(UTC)
            try:
                async with async_session_factory() as session:
                    await record_background_job_run(
                        session,
                        job_type="housekeeping",
                        scope="platform",
                        status="failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        error=str(exc),
                    )
            except Exception:
                logger.exception("Failed to persist housekeeping failure record")
        await asyncio.sleep(interval)
