"""Heartbeat Monitor — lost task detection.

Background task that periodically scans running tasks whose heartbeat
column hasn't been updated within the timeout period. Marks expired tasks
as 'lost' and cleans up their containers.

Borrowed from OpenClaw's reliable task queue pattern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from session_manager.runtime_launchers import get_runtime_launcher
from session_manager.workflow_config import load_agent_yaml
from sqlalchemy import select

from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.message_bus import post_channel_message
from shared.lib.models import Session, Task, TaskEvent
from shared.lib.task_queue import waiting_timeout_sweep

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 30
DEFAULT_LOST_TIMEOUT_SEC = 300


async def heartbeat_monitor() -> None:
    """Periodically sweep for lost tasks and notify through the message bus."""
    logger.info("Heartbeat monitor started (sweep every %ds)", SWEEP_INTERVAL_SEC)

    while True:
        try:
            async with async_session_factory() as session:
                lost_ids = await _lost_task_sweep(session)
                timed_out_ids = await _runtime_timeout_sweep(session)
                waiting_timed_out_ids = await waiting_timeout_sweep(session)

            if lost_ids:
                logger.warning("Detected %d lost task(s): %s", len(lost_ids), lost_ids)
                for task_id in lost_ids:
                    await _notify_lost_task(task_id)
                    await _cleanup_lost_container(task_id)

            if timed_out_ids:
                logger.warning("Detected %d runtime-timed-out task(s): %s", len(timed_out_ids), timed_out_ids)
                for task_id in timed_out_ids:
                    await _notify_timed_out_task(task_id)
                    await _cleanup_lost_container(task_id)

            if waiting_timed_out_ids:
                logger.warning(
                    "Detected %d waiting-timed-out task(s): %s",
                    len(waiting_timed_out_ids),
                    waiting_timed_out_ids,
                )
                for task_id in waiting_timed_out_ids:
                    await _notify_timed_out_task(task_id)
                    await _cleanup_lost_container(task_id)

        except Exception:
            logger.exception("Error in heartbeat monitor")

        await asyncio.sleep(SWEEP_INTERVAL_SEC)


async def _notify_lost_task(task_id) -> None:
    """Post notification to the message bus about a lost task."""
    if not settings.message_bus_api_url:
        return

    # Look up task for MM channel info
    from shared.lib.task_queue import get_task

    async with async_session_factory() as session:
        task = await get_task(session, task_id)

    if not task or not task.message_channel:
        return

    bot_token = settings.message_bus_bot_token
    if not bot_token:
        logger.warning(
            "Skipping lost-task message post for %s: platform MESSAGE_BUS_BOT_TOKEN not configured",
            task.workflow,
        )
        return

    lost_timeout_sec = _workflow_lost_timeout_sec(task.workflow)

    text = (
        f":warning: **Task Lost** — `{str(task_id)[:8]}`\n"
        f"Investigation lost — agent container did not respond within "
        f"{lost_timeout_sec // 60 if lost_timeout_sec >= 60 else lost_timeout_sec}"
        f"{' minutes' if lost_timeout_sec >= 60 else ' seconds'}.\n"
        f"Workflow: {task.workflow}"
    )

    metadata = task.task_metadata if isinstance(task.task_metadata, dict) else {}

    team_name = str(metadata.get("team_domain") or metadata.get("team_name") or settings.message_bus_team_name or "")
    posted = await post_channel_message(
        settings.message_bus_provider,
        api_url=settings.message_bus_api_url,
        bot_token=bot_token,
        text=text,
        channel_id=str(metadata.get("channel_id") or ""),
        channel_name=task.message_channel or "",
        team_id=str(metadata.get("team_id") or ""),
        team_name=team_name,
        thread_root=task.message_thread or "",
    )
    if posted is None:
        logger.warning("Failed to notify about lost task %s", task_id)


async def _notify_timed_out_task(task_id) -> None:
    """Post notification to the message bus about a task exceeding runtime timeout."""
    if not settings.message_bus_api_url:
        return

    from shared.lib.task_queue import get_task

    async with async_session_factory() as session:
        task = await get_task(session, task_id)

    if not task or not task.message_channel:
        return

    bot_token = settings.message_bus_bot_token
    if not bot_token:
        logger.warning(
            "Skipping timed-out task message post for %s: platform MESSAGE_BUS_BOT_TOKEN not configured",
            task.workflow,
        )
        return

    metadata = task.task_metadata if isinstance(task.task_metadata, dict) else {}
    timeout_sec = _workflow_runtime_timeout_sec(task.workflow)
    text = (
        f":warning: **Task Timed Out** — `{str(task_id)[:8]}`\n"
        f"Investigation exceeded the configured runtime timeout of "
        f"{timeout_sec // 60 if timeout_sec >= 60 else timeout_sec}"
        f"{' minutes' if timeout_sec >= 60 else ' seconds'}.\n"
        f"Workflow: {task.workflow}"
    )

    team_name = str(metadata.get("team_domain") or metadata.get("team_name") or settings.message_bus_team_name or "")
    posted = await post_channel_message(
        settings.message_bus_provider,
        api_url=settings.message_bus_api_url,
        bot_token=bot_token,
        text=text,
        channel_id=str(metadata.get("channel_id") or ""),
        channel_name=task.message_channel or "",
        team_id=str(metadata.get("team_id") or ""),
        team_name=team_name,
        thread_root=task.message_thread or "",
    )
    if posted is None:
        logger.warning("Failed to notify about timed-out task %s", task_id)


def _workflow_runtime_timeout_sec(workflow: str) -> int:
    config = load_agent_yaml(workflow) or {}
    runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
    try:
        return int(
            runtime.get(
                "runtime_timeout_sec",
                runtime.get("lost_task_timeout_sec", DEFAULT_LOST_TIMEOUT_SEC),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_LOST_TIMEOUT_SEC


def _workflow_lost_timeout_sec(workflow: str) -> int:
    config = load_agent_yaml(workflow) or {}
    runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
    try:
        return int(runtime.get("lost_task_timeout_sec", DEFAULT_LOST_TIMEOUT_SEC))
    except (TypeError, ValueError):
        return DEFAULT_LOST_TIMEOUT_SEC


async def _latest_task_session(session, task_id):
    result = await session.execute(
        select(Session).where(Session.task_id == task_id).order_by(Session.started.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _lost_task_sweep(session) -> list:
    """Mark tasks lost when heartbeats expire, with per-workflow overrides."""
    result = await session.execute(select(Task).where(Task.status == "running"))
    running_tasks = list(result.scalars().all())
    now = datetime.now(UTC)
    lost_ids = []

    for task in running_tasks:
        lost_timeout_sec = _workflow_lost_timeout_sec(task.workflow)
        heartbeat_at = task.heartbeat
        if lost_timeout_sec <= 0 or heartbeat_at is None:
            continue

        cutoff = now - timedelta(seconds=lost_timeout_sec)
        if heartbeat_at >= cutoff:
            continue

        error_msg = f"Heartbeat expired (>{lost_timeout_sec}s without update)"
        task.status = "lost"
        task.error = error_msg

        db_session = await _latest_task_session(session, task.id)
        if db_session and db_session.ended is None:
            runtime_sec = (now - db_session.started).total_seconds() if db_session.started else None
            db_session.status = "lost"
            db_session.ended = now
            db_session.duration_sec = runtime_sec
            db_session.error = error_msg

        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="task_lost",
                data={
                    "timeout_sec": lost_timeout_sec,
                    "last_heartbeat": heartbeat_at.isoformat(),
                    "session_id": str(db_session.id) if db_session else None,
                },
            )
        )
        lost_ids.append(task.id)
        logger.warning(
            "Task %s marked as lost (heartbeat %.1fs old > %ss)",
            task.id,
            (now - heartbeat_at).total_seconds(),
            lost_timeout_sec,
        )

    if lost_ids:
        await session.commit()

    return lost_ids


async def _runtime_timeout_sweep(session) -> list:
    """Mark tasks timed_out if total runtime exceeds workflow timeout.

    This complements heartbeat expiry: a task that keeps heartbeating but never
    finishes should still be reaped after the workflow's configured runtime
    ceiling.
    """
    result = await session.execute(select(Task).where(Task.status == "running"))
    running_tasks = list(result.scalars().all())
    now = datetime.now(UTC)
    timed_out_ids = []

    for task in running_tasks:
        timeout_sec = _workflow_runtime_timeout_sec(task.workflow)
        if timeout_sec <= 0:
            continue

        db_session = await _latest_task_session(session, task.id)
        started_at = db_session.started if db_session and db_session.started else task.created
        if not started_at:
            continue

        runtime_sec = (now - started_at).total_seconds()
        if runtime_sec <= timeout_sec:
            continue

        error_msg = f"Runtime exceeded configured timeout (>{timeout_sec}s)"
        task.status = "timed_out"
        task.error = error_msg
        task.duration_sec = runtime_sec

        if db_session and db_session.ended is None:
            db_session.status = "timed_out"
            db_session.ended = now
            db_session.duration_sec = runtime_sec
            db_session.error = error_msg

        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="task_timed_out",
                data={
                    "timeout_sec": timeout_sec,
                    "runtime_sec": runtime_sec,
                    "started_at": started_at.isoformat(),
                    "session_id": str(db_session.id) if db_session else None,
                },
            )
        )
        timed_out_ids.append(task.id)
        logger.warning("Task %s marked as timed_out (runtime %.1fs > %ss)", task.id, runtime_sec, timeout_sec)

    if timed_out_ids:
        await session.commit()

    return timed_out_ids


async def _cleanup_lost_container(task_id) -> None:
    """Try to stop and remove the runtime execution for a lost task."""
    try:
        get_runtime_launcher().cancel(task_id=str(task_id))
    except Exception:
        logger.warning("Failed to cleanup runtime for lost task %s", task_id)
