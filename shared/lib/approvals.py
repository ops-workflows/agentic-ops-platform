"""Helpers for persisting approval events as first-class records."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.models import Approval, SessionEvent, Task


def _merge_metadata(existing: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    merged.update(updates)
    return merged


async def _find_open_approval(
    session: AsyncSession,
    task_id: uuid.UUID,
    tool_name: str,
) -> Approval | None:
    result = await session.execute(
        select(Approval)
        .where(
            Approval.task_id == task_id,
            Approval.tool_name == tool_name,
            Approval.status == "pending",
        )
        .order_by(Approval.requested_at.desc(), Approval.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_approval_by_request(
    session: AsyncSession,
    task_id: uuid.UUID,
    tool_name: str,
    request_id: str,
) -> Approval | None:
    result = await session.execute(
        select(Approval)
        .where(
            Approval.task_id == task_id,
            Approval.tool_name == tool_name,
        )
        .order_by(Approval.requested_at.desc(), Approval.created_at.desc())
    )
    for approval in result.scalars().all():
        payload = approval.approval_metadata or {}
        if str(payload.get("approval_requested", {}).get("request_id") or "") == request_id:
            return approval
    return None


def _approval_status_from_result(data: dict[str, Any]) -> str:
    return "approved" if bool(data.get("approved")) else "rejected"


async def apply_approval_event(
    session: AsyncSession,
    task: Task | None,
    task_id: uuid.UUID | None,
    event_type: str,
    event_timestamp: datetime,
    data: dict[str, Any] | None,
) -> Approval | None:
    if not task_id:
        return None

    payload = data or {}
    tool_name = str(payload.get("tool_name") or "").strip()
    if not tool_name:
        return None

    approval: Approval | None = None

    if event_type == "permission_callback":
        if payload.get("kind") != "operator_approval":
            return None

        approval = await _find_open_approval(session, task_id, tool_name)
        if approval is None:
            approval = Approval(
                task_id=task_id,
                workflow=task.workflow if task else None,
                approval_kind=str(payload.get("kind") or "operator_approval"),
                tool_name=tool_name,
                status="pending",
                approval_metadata={"permission_callback": payload},
                requested_at=event_timestamp,
                updated_at=event_timestamp,
            )
            session.add(approval)
        else:
            approval.approval_kind = str(payload.get("kind") or approval.approval_kind)
            approval.approval_metadata = _merge_metadata(approval.approval_metadata, {"permission_callback": payload})
            approval.updated_at = event_timestamp
        return approval

    if event_type == "approval_requested":
        approval = await _find_open_approval(session, task_id, tool_name)
        preview = payload.get("tool_input_preview")
        if approval is None:
            approval = Approval(
                task_id=task_id,
                workflow=task.workflow if task else None,
                approval_kind="operator_approval",
                tool_name=tool_name,
                status="pending",
                request_preview=str(preview) if preview else None,
                approval_metadata={"approval_requested": payload},
                requested_at=event_timestamp,
                updated_at=event_timestamp,
            )
            session.add(approval)
        else:
            if preview:
                approval.request_preview = str(preview)
            approval.approval_metadata = _merge_metadata(approval.approval_metadata, {"approval_requested": payload})
            approval.updated_at = event_timestamp
        return approval

    if event_type == "approval_result":
        approval = await _find_open_approval(session, task_id, tool_name)
        if approval is None:
            approval = Approval(
                task_id=task_id,
                workflow=task.workflow if task else None,
                approval_kind="operator_approval",
                tool_name=tool_name,
                status=_approval_status_from_result(payload),
                reason=str(payload.get("reason") or "") or None,
                approval_metadata={"approval_result": payload},
                requested_at=event_timestamp,
                resolved_at=event_timestamp,
                updated_at=event_timestamp,
            )
            session.add(approval)
        else:
            approval.status = _approval_status_from_result(payload)
            approval.reason = str(payload.get("reason") or "") or None
            approval.approval_metadata = _merge_metadata(approval.approval_metadata, {"approval_result": payload})
            approval.resolved_at = event_timestamp
            approval.updated_at = event_timestamp
        return approval

    return None


async def backfill_approvals_for_task_ids(
    session: AsyncSession,
    task_ids: Sequence[uuid.UUID],
) -> int:
    normalized_ids = [task_id for task_id in task_ids if task_id]
    if not normalized_ids:
        return 0

    task_result = await session.execute(select(Task).where(Task.id.in_(normalized_ids)))
    tasks = {task.id: task for task in task_result.scalars().all()}

    await session.execute(delete(Approval).where(Approval.task_id.in_(normalized_ids)))

    event_result = await session.execute(
        select(SessionEvent)
        .where(
            SessionEvent.task_id.in_(normalized_ids),
            SessionEvent.event_type.in_(("permission_callback", "approval_requested", "approval_result")),
        )
        .order_by(SessionEvent.task_id, SessionEvent.timestamp, SessionEvent.id)
    )

    created = 0
    for event in event_result.scalars().all():
        approval = await apply_approval_event(
            session,
            tasks.get(event.task_id),
            event.task_id,
            event.event_type,
            event.timestamp or datetime.now(UTC),
            event.data,
        )
        if approval is not None:
            created += 1

    return created
