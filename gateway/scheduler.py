"""Scheduler — APScheduler cron jobs from agent.yaml.

Reads cron definitions from agent.yaml schedules section and creates
tasks in the Postgres queue when cron fires.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from shared.lib.db import async_session_factory
from shared.lib.models import Agent, Schedule
from shared.lib.task_queue import create_task

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _job_id(agent_name: str, schedule_name: str) -> str:
    return f"{agent_name}:{schedule_name}"


def _resolve_schedule_channel(agent_config: dict[str, Any], schedule_name: str) -> str | None:
    schedules = agent_config.get("schedules", []) if isinstance(agent_config, dict) else []
    for sched in schedules:
        if not isinstance(sched, dict):
            continue
        if sched.get("name") == schedule_name and sched.get("message_channel"):
            return str(sched["message_channel"])

    messaging = {}
    if isinstance(agent_config, dict):
        messaging = agent_config.get("messaging") or {}
    channels = messaging.get("channels", []) if isinstance(messaging, dict) else []
    if not isinstance(channels, list):
        return None

    normalized_name = schedule_name.replace("_", "-")
    for channel in channels:
        if channel == normalized_name:
            return str(channel)

    return str(channels[0]) if channels else None


async def _schedule_job_handler(agent_name: str, schedule_name: str, prompt: str, message_channel: str | None) -> None:
    """Handler for a scheduled cron job — creates a task in the queue."""
    logger.info("Schedule fired: agent=%s schedule=%s", agent_name, schedule_name)

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=agent_name,
            prompt=prompt,
            channel="schedule",
            message_channel=message_channel,
            metadata={
                "schedule": schedule_name,
                "triggered_by": "scheduler",
            },
        )

        # Update last_run on the schedule
        result = await session.execute(
            select(Schedule)
            .join(Agent, Schedule.agent_id == Agent.id)
            .where(Agent.name == agent_name, Schedule.name == schedule_name)
        )
        sched = result.scalar_one_or_none()
        if sched:
            now = datetime.now(UTC)
            sched.last_run = now
            sched.next_run = _next_run_at(sched.cron, now)
            await session.commit()

    logger.info("Created scheduled task %s for %s/%s", task.id, agent_name, schedule_name)


def _parse_cron(cron_str: str) -> CronTrigger:
    """Parse a cron string into an APScheduler CronTrigger.

    Supports standard 5-field cron: minute hour day_of_month month day_of_week
    """
    parts = cron_str.strip().strip('"').strip("'").split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {cron_str}")
    return CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
    )


def compute_next_run(cron_str: str) -> str | None:
    """Compute the next fire time from a cron expression. Returns ISO string or None."""
    try:
        next_fire = _next_run_at(cron_str)
        return next_fire.isoformat() if next_fire else None
    except Exception:
        return None


def _next_run_at(cron_str: str, after: datetime | None = None) -> datetime | None:
    """Return the next UTC fire time for a cron expression."""
    trigger = _parse_cron(cron_str)
    reference = after or datetime.now(UTC)
    return trigger.get_next_fire_time(None, reference)


async def _persist_next_runs() -> None:
    """Mirror in-memory APScheduler timing into persisted schedule status."""
    if _scheduler is None:
        return

    async with async_session_factory() as session:
        result = await session.execute(
            select(Schedule, Agent.name).join(Agent, Schedule.agent_id == Agent.id).where(Schedule.enabled == True)  # noqa: E712
        )
        for schedule, agent_name in result.all():
            job = _scheduler.get_job(_job_id(agent_name, schedule.name))
            schedule.next_run = job.next_run_time if job else None
        await session.commit()


async def start_scheduler() -> None:
    """Start the APScheduler and register cron jobs from all agents."""
    global _scheduler
    _scheduler = AsyncIOScheduler()

    async with async_session_factory() as session:
        result = await session.execute(
            select(Schedule, Agent.name, Agent.config)
            .join(Agent, Schedule.agent_id == Agent.id)
            .where(Schedule.enabled == True)  # noqa: E712
        )
        schedules = result.all()

    for sched, agent_name, agent_config in schedules:
        try:
            register_schedule_job(
                agent_name=agent_name,
                schedule_name=sched.name,
                cron=sched.cron,
                prompt=sched.prompt or "",
                agent_config=agent_config or {},
            )
        except Exception:
            logger.exception("Failed to register schedule %s/%s", agent_name, sched.name)

    _scheduler.start()
    await _persist_next_runs()
    logger.info("Scheduler started with %d job(s)", len(_scheduler.get_jobs()))


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def register_schedule_job(
    *,
    agent_name: str,
    schedule_name: str,
    cron: str,
    prompt: str,
    agent_config: dict[str, Any],
) -> bool:
    if _scheduler is None:
        logger.warning("Scheduler not started; skipped registering %s/%s", agent_name, schedule_name)
        return False

    trigger = _parse_cron(cron)
    message_channel = _resolve_schedule_channel(agent_config or {}, schedule_name)
    _scheduler.add_job(
        _schedule_job_handler,
        trigger=trigger,
        args=[agent_name, schedule_name, prompt, message_channel],
        id=_job_id(agent_name, schedule_name),
        name=f"{agent_name}/{schedule_name}",
        replace_existing=True,
    )
    logger.info("Registered schedule: %s/%s (cron=%s)", agent_name, schedule_name, cron)
    return True


def unregister_schedule_job(*, agent_name: str, schedule_name: str) -> bool:
    if _scheduler is None:
        logger.warning("Scheduler not started; skipped unregistering %s/%s", agent_name, schedule_name)
        return False

    job = _scheduler.get_job(_job_id(agent_name, schedule_name))
    if job is None:
        return False

    _scheduler.remove_job(job.id)
    logger.info("Unregistered schedule: %s/%s", agent_name, schedule_name)
    return True
