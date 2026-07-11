"""Non-agentic retention and archive housekeeping."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.config import settings
from shared.lib.memory_catalog import BANK_WORKFLOW_LEARNING, load_workflow_banks
from shared.lib.models import Approval, BackgroundJobRun, Session, SessionEvent, Task, TaskEvent
from shared.lib.object_store import BUCKET_AGENT_MEMORY, delete_object, list_objects
from shared.lib.task_queue import TERMINAL_STATUSES

logger = logging.getLogger(__name__)


@dataclass
class HousekeepingReport:
    archived_tasks: int = 0
    deleted_tasks: int = 0
    pruned_agent_memory_versions: int = 0
    pruned_learning_memories: int = 0
    pruned_background_job_runs: int = 0
    warnings: list[str] = field(default_factory=list)


def learning_bank_ids() -> list[str]:
    bank_ids = {BANK_WORKFLOW_LEARNING}
    bank_ids.update(load_workflow_banks().get("learning", {}).values())
    return sorted(bank_ids)


def _parse_hindsight_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _extract_memory_id(item: dict[str, Any]) -> str | None:
    for key in ("id", "memory_id", "document_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return None


def _extract_document_id(item: dict[str, Any]) -> str | None:
    value = str(item.get("document_id") or "").strip()
    if value:
        return value
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        value = str(metadata.get("document_id") or "").strip()
        if value:
            return value
    return None


def _should_skip_memory_invalidation(item: dict[str, Any]) -> bool:
    fact_type = str(item.get("fact_type") or item.get("type") or item.get("kind") or "").strip().lower()
    return fact_type == "observation" and not _extract_document_id(item)


async def _list_hindsight_memories(client: httpx.AsyncClient, bank_id: str) -> dict[str, Any]:
    list_url = f"{settings.hindsight_url}/v1/default/banks/{bank_id}/memories/list"
    attempts = max(1, int(settings.hindsight_request_retries or 1))
    retry_backoff = max(0.0, float(settings.hindsight_request_retry_backoff_sec or 0.0))

    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(list_url, params={"limit": 500, "offset": 0})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("expected object payload")
            return payload
        except httpx.HTTPStatusError:
            raise
        except httpx.TransportError:
            if attempt >= attempts:
                raise
            if retry_backoff > 0:
                await asyncio.sleep(retry_backoff * attempt)

    return {}


async def archive_terminal_tasks(
    session: AsyncSession,
    *,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> int:
    days = settings.task_archive_after_days if older_than_days is None else older_than_days
    if days <= 0:
        return 0
    anchor = now or datetime.now(UTC)
    cutoff = anchor - timedelta(days=days)

    result = await session.execute(
        select(Task).where(
            Task.status.in_(TERMINAL_STATUSES),
            Task.archived_at.is_(None),
            Task.created < cutoff,
        )
    )
    tasks = list(result.scalars().all())
    if not tasks:
        return 0

    archived_at = anchor
    task_ids = [task.id for task in tasks]
    for task in tasks:
        task.archived_at = archived_at
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="task_auto_archived",
                data={"cutoff": cutoff.isoformat(), "status": task.status},
            )
        )

    await session.execute(update(Session).where(Session.task_id.in_(task_ids)).values(archived_at=archived_at))
    await session.execute(
        update(SessionEvent).where(SessionEvent.task_id.in_(task_ids)).values(archived_at=archived_at)
    )
    await session.execute(update(Approval).where(Approval.task_id.in_(task_ids)).values(archived_at=archived_at))
    await session.commit()
    return len(task_ids)


async def delete_archived_tasks(
    session: AsyncSession,
    *,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> int:
    days = settings.task_delete_after_days if older_than_days is None else older_than_days
    if days <= 0:
        return 0
    anchor = now or datetime.now(UTC)
    cutoff = anchor - timedelta(days=days)

    result = await session.execute(select(Task.id).where(Task.archived_at.isnot(None), Task.archived_at < cutoff))
    task_ids = [row[0] for row in result.all()]
    if not task_ids:
        return 0

    await session.execute(delete(Approval).where(Approval.task_id.in_(task_ids)))
    await session.execute(delete(SessionEvent).where(SessionEvent.task_id.in_(task_ids)))
    await session.execute(delete(Session).where(Session.task_id.in_(task_ids)))
    await session.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
    await session.execute(delete(Task).where(Task.id.in_(task_ids)))
    await session.commit()
    return len(task_ids)


async def prune_learning_bank_memories(
    *,
    retention_days: int | None = None,
    bank_ids: list[str] | None = None,
    now: datetime | None = None,
) -> tuple[int, list[str]]:
    days = settings.learning_memory_retention_days if retention_days is None else retention_days
    if days <= 0:
        return 0, []
    anchor = now or datetime.now(UTC)
    cutoff = anchor - timedelta(days=days)
    target_banks = bank_ids or learning_bank_ids()
    deleted_count = 0
    warnings: list[str] = []
    invalidation_reason = f"housekeeping retention: older than {days} days"

    async with httpx.AsyncClient(timeout=10.0) as client:
        for bank_id in target_banks:
            try:
                payload = await _list_hindsight_memories(client, bank_id)
            except (httpx.HTTPError, ValueError) as exc:
                warning = f"Failed to list Hindsight learning bank {bank_id}: {exc}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            items = payload.get("items") or payload.get("memories") or payload.get("results") or payload.get("data")
            if not isinstance(items, list):
                continue

            deleted_document_ids: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                created_at = _parse_hindsight_timestamp(
                    item.get("created_at") or item.get("timestamp") or item.get("updated_at") or item.get("date")
                )
                memory_id = _extract_memory_id(item)
                if not created_at or not memory_id or created_at >= cutoff:
                    continue

                document_id = _extract_document_id(item)
                if document_id:
                    if document_id in deleted_document_ids:
                        continue
                    deleted_document_ids.add(document_id)
                    delete_url = f"{settings.hindsight_url}/v1/default/banks/{bank_id}/documents/{document_id}"
                    try:
                        delete_response = await client.delete(delete_url)
                        delete_response.raise_for_status()
                        delete_payload = delete_response.json() if delete_response.content else {}
                        deleted_units = int(
                            (delete_payload.get("memory_units_deleted") if isinstance(delete_payload, dict) else 0) or 0
                        )
                        deleted_count += deleted_units if deleted_units > 0 else 1
                    except (httpx.HTTPError, ValueError) as exc:
                        warning = f"Failed to delete Hindsight learning document {document_id} in {bank_id}: {exc}"
                        logger.warning(warning)
                        warnings.append(warning)
                    continue

                if _should_skip_memory_invalidation(item):
                    continue

                update_url = f"{settings.hindsight_url}/v1/default/banks/{bank_id}/memories/{memory_id}"
                try:
                    update_response = await client.patch(
                        update_url,
                        json={"state": "invalidated", "reason": invalidation_reason},
                    )
                    update_response.raise_for_status()
                    deleted_count += 1
                except httpx.HTTPError as exc:
                    warning = f"Failed to invalidate Hindsight learning memory {memory_id} in {bank_id}: {exc}"
                    logger.warning(warning)
                    warnings.append(warning)

    return deleted_count, warnings


async def prune_background_job_runs(
    session: AsyncSession,
    *,
    job_type: str = "housekeeping",
    keep_latest: int | None = None,
) -> int:
    keep = settings.background_job_run_history_limit if keep_latest is None else keep_latest
    if keep is None:
        return 0
    keep = int(keep)
    if keep < 0:
        return 0

    result = await session.execute(
        select(BackgroundJobRun.id)
        .where(BackgroundJobRun.job_type == job_type)
        .order_by(BackgroundJobRun.started_at.desc(), BackgroundJobRun.id.desc())
        .offset(keep)
    )
    run_ids = [row[0] for row in result.all()]
    if not run_ids:
        return 0

    await session.execute(delete(BackgroundJobRun).where(BackgroundJobRun.id.in_(run_ids)))
    await session.commit()
    return len(run_ids)


def prune_agent_memory_versions(
    *,
    versions_to_keep: int | None = None,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> int:
    keep = settings.agent_memory_versions_to_keep if versions_to_keep is None else versions_to_keep
    days = settings.agent_memory_retention_days if retention_days is None else retention_days
    if keep <= 0 and days <= 0:
        return 0

    anchor = now or datetime.now(UTC)
    cutoff = anchor - timedelta(days=days) if days > 0 else None
    objects = list_objects(BUCKET_AGENT_MEMORY)
    grouped: dict[str, list] = {}
    for obj in objects:
        if obj.key.endswith("/latest.tar.gz"):
            continue
        agent_name = obj.key.split("/", 1)[0]
        grouped.setdefault(agent_name, []).append(obj)

    deleted_count = 0
    for versions in grouped.values():
        versions.sort(key=lambda item: item.last_modified or datetime.min.replace(tzinfo=UTC), reverse=True)
        for index, obj in enumerate(versions):
            older_than_keep = keep > 0 and index >= keep
            older_than_cutoff = cutoff is not None and obj.last_modified is not None and obj.last_modified < cutoff
            if (older_than_keep or older_than_cutoff) and delete_object(BUCKET_AGENT_MEMORY, obj.key):
                deleted_count += 1
    return deleted_count


async def run_housekeeping_once(session: AsyncSession | None = None) -> HousekeepingReport:
    owns_session = session is None
    if session is None:
        from shared.lib.db import async_session_factory

        session = async_session_factory()

    if owns_session:
        async with session as owned_session:
            return await run_housekeeping_once(owned_session)

    report = HousekeepingReport()
    report.archived_tasks = await archive_terminal_tasks(session)
    report.deleted_tasks = await delete_archived_tasks(session)
    report.pruned_agent_memory_versions = prune_agent_memory_versions()
    pruned, warnings = await prune_learning_bank_memories()
    report.pruned_learning_memories = pruned
    report.warnings.extend(warnings)
    if settings.background_job_run_history_limit > 0:
        report.pruned_background_job_runs = await prune_background_job_runs(
            session,
            job_type="housekeeping",
            keep_latest=max(0, int(settings.background_job_run_history_limit) - 1),
        )
    return report


async def record_background_job_run(
    session: AsyncSession,
    *,
    job_type: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    scope: str | None = None,
    summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
) -> BackgroundJobRun:
    run = BackgroundJobRun(
        job_type=job_type,
        scope=scope,
        status=status,
        started_at=started_at,
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


async def archive_task_and_related(session: AsyncSession, task_id: uuid.UUID, *, archived: bool) -> None:
    archived_at = datetime.now(UTC) if archived else None
    await session.execute(update(Task).where(Task.id == task_id).values(archived_at=archived_at))
    await session.execute(update(Session).where(Session.task_id == task_id).values(archived_at=archived_at))
    await session.execute(update(SessionEvent).where(SessionEvent.task_id == task_id).values(archived_at=archived_at))
    await session.execute(update(Approval).where(Approval.task_id == task_id).values(archived_at=archived_at))
    await session.commit()
