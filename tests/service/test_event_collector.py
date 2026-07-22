"""Layer 1 — event collector end-to-end."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.service

from sqlalchemy import select  # noqa: E402

from gateway.event_collector import EventPayload, receive_event  # noqa: E402
from shared.lib.models import Approval, SessionEvent, Task  # noqa: E402
from shared.lib.task_queue import create_task, dequeue_task  # noqa: E402


def _ts() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_session_complete_marks_task_succeeded(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="p")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="session_complete",
            timestamp=_ts(),
            data={
                "input_tokens": 100,
                "output_tokens": 50,
                "duration_sec": 1.25,
                "result": "ok",
            },
        )
    )
    refreshed = await db_session.get(Task, task.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "succeeded"
    assert refreshed.tokens_used == 150
    assert refreshed.duration_sec == 1.25


@pytest.mark.asyncio
async def test_session_error_marks_failed(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="p")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="session_error",
            timestamp=_ts(),
            data={"error": "boom", "input_tokens": 5, "output_tokens": 0, "duration_sec": 0.2},
        )
    )
    refreshed = await db_session.get(Task, task.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "failed"
    assert "boom" in (refreshed.error or "")


@pytest.mark.asyncio
async def test_session_timeout_marks_timed_out(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="p")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="session_timeout",
            timestamp=_ts(),
            data={"error": "too long", "duration_sec": 120.0},
        )
    )
    refreshed = await db_session.get(Task, task.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "timed_out"


@pytest.mark.asyncio
async def test_approval_requested_creates_pending_approval(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="p")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="approval_requested",
            timestamp=_ts(),
            data={
                "tool_name": "Bash",
                "tool_input_preview": "echo approval-needed hi",
            },
        )
    )
    approvals = (await db_session.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].tool_name == "Bash"


@pytest.mark.asyncio
async def test_waiting_task_frees_running_slot(db_session) -> None:
    waiting_task = await create_task(db_session, workflow="platform-test", prompt="needs approval")
    queued_task = await create_task(db_session, workflow="platform-test", prompt="can run next")

    dequeued = await dequeue_task(db_session, workflow="platform-test", max_running=1)
    assert dequeued is not None
    assert dequeued.id == waiting_task.id

    await receive_event(
        EventPayload(
            task_id=str(waiting_task.id),
            event_type="approval_requested",
            timestamp=_ts(),
            data={
                "tool_name": "Bash",
                "tool_input_preview": "echo approval-needed hi",
            },
        )
    )

    refreshed = await db_session.get(Task, waiting_task.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "waiting_approval"

    next_task = await dequeue_task(db_session, workflow="platform-test", max_running=1)
    assert next_task is not None
    assert next_task.id == queued_task.id


@pytest.mark.asyncio
async def test_wait_resolution_moves_task_to_resume_pending(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="wait then continue")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="user_question_requested",
            timestamp=_ts(),
            data={"questions": [{"question": "Proceed?"}]},
        )
    )

    waiting = await db_session.get(Task, task.id)
    await db_session.refresh(waiting)
    assert waiting.status == "waiting_user_input"

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="user_question_resolved",
            timestamp=_ts(),
            data={"question_count": 1},
        )
    )

    resumed = await db_session.get(Task, task.id)
    await db_session.refresh(resumed)
    assert resumed.status == "resume_pending"
    assert resumed.wait_reason is None
    assert resumed.wait_deadline is None

    redequeued = await dequeue_task(db_session, workflow="platform-test", max_running=1)
    assert redequeued is not None
    assert redequeued.id == task.id
    assert redequeued.status == "running"


@pytest.mark.asyncio
async def test_conversation_batch_updates_tokens(db_session) -> None:
    task = await create_task(db_session, workflow="platform-test", prompt="p")
    await dequeue_task(db_session, workflow="platform-test")

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="conversation_batch",
            timestamp=_ts(),
            data={
                "messages": [
                    {
                        "type": "system",
                        "subtype": "task_progress",
                        "data": {"usage": {"total_tokens": 77}},
                    }
                ]
            },
        )
    )
    refreshed = await db_session.get(Task, task.id)
    await db_session.refresh(refreshed)
    assert refreshed.tokens_used == 77

    await receive_event(
        EventPayload(
            task_id=str(task.id),
            event_type="conversation_batch",
            timestamp=_ts(),
            data={
                "messages": [
                    {
                        "type": "system",
                        "subtype": "task_progress",
                        "data": {"usage": {"total_tokens": 0}},
                    }
                ]
            },
        )
    )
    await db_session.refresh(refreshed)
    assert refreshed.tokens_used == 77


@pytest.mark.asyncio
async def test_event_with_empty_task_id_is_safe(db_session) -> None:
    """Events without a task context should be stored without FK violations."""
    resp = await receive_event(
        EventPayload(
            task_id="",
            event_type="session_progress",
            timestamp=_ts(),
            data={"note": "ambient"},
        )
    )
    assert resp == {"status": "ok"}
    events = (
        (await db_session.execute(select(SessionEvent).where(SessionEvent.event_type == "session_progress")))
        .scalars()
        .all()
    )
    assert events
