"""Event Collector — replaces Langfuse.

Receives structured events from the runtime harness inside session
containers and writes them to Postgres.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from gateway.approval_broker import ensure_approval_prompt_posted
from shared.lib.approvals import apply_approval_event
from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.models import SessionEvent, Task

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_INLINE_SIZE = 10 * 1024  # 10KB
TOKEN_TOTAL_PATTERN = re.compile(r"total_tokens['\"]?\s*[:=]\s*(\d+)")


class EventPayload(BaseModel):
    task_id: str = ""
    event_type: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    data: dict[str, Any] = Field(default_factory=dict)


def _coerce_total_tokens(usage: Any) -> int | None:
    if isinstance(usage, dict):
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, (int, float)):
            return int(total_tokens)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, (int, float)) or isinstance(output_tokens, (int, float)):
            return int(input_tokens or 0) + int(output_tokens or 0)
        return None

    if isinstance(usage, str):
        stripped = usage.strip()
        if not stripped:
            return None

        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(stripped)
            except Exception as exc:
                logger.debug("Failed to parse usage token payload", exc_info=exc)
                continue
            total_tokens = _coerce_total_tokens(parsed)
            if total_tokens is not None:
                return total_tokens

        match = TOKEN_TOTAL_PATTERN.search(stripped)
        if match:
            return int(match.group(1))

    return None


def _extract_incremental_tokens(event_data: dict[str, Any]) -> int | None:
    messages = event_data.get("messages")
    if not isinstance(messages, list):
        return None

    latest_total: int | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue

        if message.get("type") == "result":
            total_tokens = _coerce_total_tokens(message.get("usage"))
            if total_tokens is not None:
                latest_total = total_tokens
            continue

        if message.get("type") != "system" or message.get("subtype") != "task_progress":
            continue

        payload = message.get("data")
        if not isinstance(payload, dict):
            continue

        total_tokens = _coerce_total_tokens(payload.get("usage"))
        if total_tokens is not None:
            latest_total = total_tokens

    return latest_total


def _can_deliver_approval_prompt(task: Task | None) -> bool:
    if task is None:
        return False
    task_metadata = task.task_metadata if isinstance(task.task_metadata, dict) else {}
    return bool(
        settings.message_bus_api_url
        and settings.message_bus_bot_token
        and (task.message_channel or task.message_thread or task_metadata.get("channel_id"))
    )


@router.post("/events")
async def receive_event(event: EventPayload):
    """Receive an event from an agent session.

    Writes event data to Postgres (control_plane.session_events).
    """
    # Write event to Postgres
    try:
        task_uuid = uuid.UUID(event.task_id) if event.task_id else None
    except ValueError:
        task_uuid = None

    async with async_session_factory() as session:
        db_event = SessionEvent(
            task_id=task_uuid,
            event_type=event.event_type,
            timestamp=datetime.fromisoformat(event.timestamp),
            data=event.data,
        )
        session.add(db_event)

        if task_uuid:
            task = await session.scalar(select(Task).where(Task.id == task_uuid).with_for_update())
            task_updates: dict[str, Any] = {
                "heartbeat": datetime.now(UTC),
            }

            approval = await apply_approval_event(
                session,
                task,
                task_uuid,
                event.event_type,
                datetime.fromisoformat(event.timestamp),
                event.data,
            )

            if event.event_type == "approval_requested":
                if _can_deliver_approval_prompt(task):
                    await ensure_approval_prompt_posted(session, task, approval)
                if approval is not None and approval.status == "pending":
                    task_updates.update(
                        {
                            "status": "waiting_approval",
                            "wait_reason": "operator_approval",
                            "wait_deadline": datetime.now(UTC) + timedelta(seconds=3600),
                        }
                    )

            if event.event_type == "user_question_requested":
                task_updates.update(
                    {
                        "status": "waiting_user_input",
                        "wait_reason": "ask_user_question",
                        "wait_deadline": datetime.now(UTC) + timedelta(seconds=3600),
                    }
                )

            if event.event_type in {"user_question_resolved", "approval_wait_resolved"}:
                task_updates.update(
                    {
                        "status": "resume_pending",
                        "wait_reason": None,
                        "wait_deadline": None,
                    }
                )

            if event.event_type == "conversation_batch":
                incremental_tokens = _extract_incremental_tokens(event.data)
                if incremental_tokens is not None:
                    task_updates["tokens_used"] = incremental_tokens

            if event.event_type == "session_complete":
                input_tokens = int(event.data.get("input_tokens", 0) or 0)
                output_tokens = int(event.data.get("output_tokens", 0) or 0)
                task_updates.update(
                    {
                        "status": "succeeded",
                        "result": event.data,
                        "tokens_used": input_tokens + output_tokens,
                        "duration_sec": event.data.get("duration_sec"),
                        "error": None,
                    }
                )
            elif event.event_type == "session_error":
                input_tokens = int(event.data.get("input_tokens", 0) or 0)
                output_tokens = int(event.data.get("output_tokens", 0) or 0)
                task_updates.update(
                    {
                        "status": "failed",
                        "result": event.data,
                        "duration_sec": event.data.get("duration_sec"),
                        "error": str(event.data.get("error") or "Session error"),
                    }
                )
                if input_tokens or output_tokens:
                    task_updates["tokens_used"] = input_tokens + output_tokens
            elif event.event_type == "session_timeout":
                input_tokens = int(event.data.get("input_tokens", 0) or 0)
                output_tokens = int(event.data.get("output_tokens", 0) or 0)
                task_updates.update(
                    {
                        "status": "timed_out",
                        "result": event.data,
                        "duration_sec": event.data.get("duration_sec"),
                        "error": str(event.data.get("error") or "Session timed out"),
                    }
                )
                if input_tokens or output_tokens:
                    task_updates["tokens_used"] = input_tokens + output_tokens

            await session.execute(update(Task).where(Task.id == task_uuid).values(**task_updates))

        await session.commit()

    return {"status": "ok"}
