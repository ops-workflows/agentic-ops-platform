"""Postgres-backed task queue using SELECT FOR UPDATE SKIP LOCKED.

Shared by Gateway (producer), Session Manager (consumer), and Connectors (producer).
Inspired by OpenClaw's reliable queue pattern.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.models import Approval, Session, SessionEvent, Task, TaskEvent

logger = logging.getLogger(__name__)

RUNNABLE_STATUSES = ("resume_pending", "queued")
WAITING_STATUSES = ("waiting_approval", "waiting_user_input")
TERMINAL_STATUSES = ("succeeded", "failed", "lost", "timed_out")


async def create_task(
    session: AsyncSession,
    *,
    workflow: str,
    prompt: str,
    channel: str | None = None,
    metadata: dict | None = None,
    message_channel: str | None = None,
    message_thread: str | None = None,
    coalesce_key: str | None = None,
    coalesce_window_sec: int = 300,
) -> Task:
    """Create a new task, with optional alert coalescing.

    If coalesce_key is provided, checks for recent queued/running tasks with the
    same key. If found within the window, appends to the existing task instead.
    """
    if coalesce_key:
        cutoff = datetime.now(UTC) - timedelta(seconds=coalesce_window_sec)
        existing = await session.execute(
            select(Task)
            .where(
                Task.coalesce_key == coalesce_key,
                Task.status.in_(["queued", "running", "resume_pending", *WAITING_STATUSES]),
                Task.created >= cutoff,
            )
            .order_by(Task.created.desc())
            .limit(1)
        )
        existing_task = existing.scalar_one_or_none()
        if existing_task:
            # Append to existing task metadata
            alerts = existing_task.task_metadata.get("coalesced_alerts", [])
            alerts.append(metadata)
            existing_task.task_metadata = {**existing_task.task_metadata, "coalesced_alerts": alerts}
            await session.flush()
            logger.info("Coalesced alert into existing task %s", existing_task.id)

            event = TaskEvent(
                task_id=existing_task.id,
                event_type="alert_coalesced",
                data={"new_metadata": metadata},
            )
            session.add(event)
            await session.commit()
            return existing_task

    task = Task(
        workflow=workflow,
        status="queued",
        prompt=prompt,
        task_metadata=metadata or {},
        channel=channel,
        message_channel=message_channel,
        message_thread=message_thread,
        coalesce_key=coalesce_key,
    )
    session.add(task)
    await session.flush()

    event = TaskEvent(
        id=uuid.uuid4(),
        task_id=task.id,
        event_type="task_created",
        data={"workflow": workflow, "metadata": metadata},
    )
    session.add(event)
    await session.commit()
    logger.info("Created task %s for workflow %s", task.id, workflow)
    return task


async def dequeue_task(
    session: AsyncSession,
    *,
    workflow: str | None = None,
    max_running: int | None = None,
) -> Task | None:
    """Dequeue the next queued task using SKIP LOCKED.

    If max_running is specified, checks concurrency limits before dequeuing.
    """
    if max_running is not None:
        count_q = select(text("count(*)")).select_from(Task).where(Task.status == "running")
        if workflow:
            count_q = count_q.where(Task.workflow == workflow)
        result = await session.execute(count_q)
        running_count = result.scalar()
        if running_count >= max_running:
            return None

    query = (
        select(Task)
        .where(Task.status.in_(RUNNABLE_STATUSES), Task.archived_at.is_(None))
        .order_by(case((Task.status == "resume_pending", 0), else_=1), Task.created)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if workflow:
        query = query.where(Task.workflow == workflow)

    result = await session.execute(query)
    task = result.scalar_one_or_none()

    if task:
        prior_status = task.status
        task.status = "running"
        task.heartbeat = datetime.now(UTC)
        task.wait_reason = None
        task.wait_deadline = None
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="task_resumed" if prior_status == "resume_pending" else "task_dequeued",
                data={"previous_status": prior_status, "workflow": task.workflow},
            )
        )
        await session.commit()
        logger.info("Dequeued task %s (workflow=%s)", task.id, task.workflow)
    return task


async def heartbeat(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Update heartbeat timestamp for a running task."""
    await session.execute(update(Task).where(Task.id == task_id).values(heartbeat=datetime.now(UTC)))
    await session.commit()


async def mark_task_waiting(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: str,
    reason: str,
    deadline: datetime | None = None,
    event_type: str | None = None,
    data: dict | None = None,
) -> None:
    if status not in WAITING_STATUSES:
        raise ValueError(f"Unsupported waiting status: {status}")
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=status,
            wait_reason=reason,
            wait_deadline=deadline,
            heartbeat=datetime.now(UTC),
        )
    )
    session.add(
        TaskEvent(
            task_id=task_id,
            event_type=event_type or f"{status}_started",
            data={"reason": reason, "deadline": deadline.isoformat() if deadline else None, **(data or {})},
        )
    )
    await session.commit()


async def mark_task_resume_pending(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    reason: str,
    event_type: str = "wait_resolved",
    data: dict | None = None,
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status="resume_pending",
            wait_reason=None,
            wait_deadline=None,
            heartbeat=datetime.now(UTC),
        )
    )
    session.add(TaskEvent(task_id=task_id, event_type=event_type, data={"reason": reason, **(data or {})}))
    await session.commit()


async def mark_task_running(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    reason: str = "resume_in_live_runtime",
    event_type: str = "task_running",
    data: dict | None = None,
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status="running",
            wait_reason=None,
            wait_deadline=None,
            heartbeat=datetime.now(UTC),
        )
    )
    session.add(TaskEvent(task_id=task_id, event_type=event_type, data={"reason": reason, **(data or {})}))
    await session.commit()


async def complete_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: str = "succeeded",
    result: dict | None = None,
    tokens_used: int = 0,
    duration_sec: float | None = None,
    error: str | None = None,
) -> None:
    """Mark a task as completed (succeeded/failed)."""
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=status,
            result=result,
            tokens_used=tokens_used,
            duration_sec=duration_sec,
            error=error,
        )
    )
    event = TaskEvent(
        task_id=task_id,
        event_type=f"task_{status}",
        data={"result": result, "tokens_used": tokens_used, "duration_sec": duration_sec, "error": error},
    )
    session.add(event)
    await session.commit()
    logger.info("Task %s completed with status %s", task_id, status)


async def maintenance_sweep(
    session: AsyncSession,
    *,
    lost_timeout_sec: int = 300,
) -> list[uuid.UUID]:
    """Mark tasks as 'lost' if heartbeat expired.

    Borrowed from OpenClaw's lost-task detection pattern.
    Returns list of task IDs marked as lost.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=lost_timeout_sec)
    result = await session.execute(
        select(Task).where(
            Task.status == "running",
            Task.heartbeat < cutoff,
        )
    )
    lost_tasks = result.scalars().all()
    lost_ids = []

    for task in lost_tasks:
        task.status = "lost"
        task.error = f"Heartbeat expired (>{lost_timeout_sec}s without update)"
        event = TaskEvent(
            task_id=task.id,
            event_type="task_lost",
            data={
                "timeout_sec": lost_timeout_sec,
                "last_heartbeat": task.heartbeat.isoformat() if task.heartbeat else None,
            },
        )
        session.add(event)
        lost_ids.append(task.id)
        logger.warning("Task %s marked as lost (heartbeat expired)", task.id)

    if lost_ids:
        await session.commit()
    return lost_ids


async def waiting_timeout_sweep(session: AsyncSession) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    result = await session.execute(
        select(Task).where(
            Task.status.in_(WAITING_STATUSES),
            Task.wait_deadline.isnot(None),
            Task.wait_deadline < now,
        )
    )
    expired = list(result.scalars().all())
    expired_ids: list[uuid.UUID] = []
    for task in expired:
        task.status = "timed_out"
        task.error = f"Waiting deadline expired for {task.wait_reason or task.status}"
        task.duration_sec = task.duration_sec
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="task_wait_timed_out",
                data={
                    "wait_reason": task.wait_reason,
                    "wait_deadline": task.wait_deadline.isoformat() if task.wait_deadline else None,
                },
            )
        )
        expired_ids.append(task.id)
    if expired_ids:
        await session.commit()
    return expired_ids


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Get a single task by ID."""
    result = await session.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()


async def list_tasks(
    session: AsyncSession,
    *,
    workflow: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    search: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    include_archived: bool = False,
    sort_by: str = "created",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks with optional filters."""
    sort_columns = {
        "created": Task.created,
        "updated": Task.updated,
        "status": Task.status,
        "workflow": Task.workflow,
    }
    sort_column = sort_columns.get(sort_by, Task.created)
    order_by = sort_column.asc() if sort_dir.lower() == "asc" else sort_column.desc()
    query = select(Task).order_by(order_by).limit(limit).offset(offset)
    if not include_archived:
        query = query.where(Task.archived_at.is_(None))
    if workflow:
        query = query.where(Task.workflow == workflow)
    if status:
        query = query.where(Task.status == status)
    if channel:
        query = query.where(Task.channel == channel)
    if created_after:
        query = query.where(Task.created >= created_after)
    if created_before:
        query = query.where(Task.created <= created_before)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Task.prompt.ilike(pattern), Task.workflow.ilike(pattern), Task.error.ilike(pattern)))
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_tasks(
    session: AsyncSession,
    *,
    workflow: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    search: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    include_archived: bool = False,
) -> int:
    query = select(func.count(Task.id))
    if not include_archived:
        query = query.where(Task.archived_at.is_(None))
    if workflow:
        query = query.where(Task.workflow == workflow)
    if status:
        query = query.where(Task.status == status)
    if channel:
        query = query.where(Task.channel == channel)
    if created_after:
        query = query.where(Task.created >= created_after)
    if created_before:
        query = query.where(Task.created <= created_before)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Task.prompt.ilike(pattern), Task.workflow.ilike(pattern), Task.error.ilike(pattern)))
    return int((await session.execute(query)).scalar() or 0)


async def archive_task(session: AsyncSession, task_id: uuid.UUID, *, archived: bool = True) -> None:
    archived_at = datetime.now(UTC) if archived else None
    await session.execute(update(Task).where(Task.id == task_id).values(archived_at=archived_at))
    await session.execute(update(Session).where(Session.task_id == task_id).values(archived_at=archived_at))
    await session.execute(update(Approval).where(Approval.task_id == task_id).values(archived_at=archived_at))
    await session.execute(update(TaskEvent).where(TaskEvent.task_id == task_id).values(archived_at=archived_at))
    await session.execute(update(SessionEvent).where(SessionEvent.task_id == task_id).values(archived_at=archived_at))
    session.add(
        TaskEvent(
            task_id=task_id,
            event_type="task_archived" if archived else "task_unarchived",
            data={"archived_at": archived_at.isoformat() if archived_at else None},
        )
    )
    await session.commit()
