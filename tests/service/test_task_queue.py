"""Layer 1 — task_queue.

Covers: create_task, dequeue with SKIP LOCKED, coalescing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.service

from shared.lib.task_queue import (  # noqa: E402
    complete_task,
    create_task,
    dequeue_task,
    heartbeat,
)


@pytest.mark.asyncio
async def test_create_task_persists_metadata_and_emits_event(db_session) -> None:
    task = await create_task(
        db_session,
        workflow="platform-test",
        prompt="hello",
        metadata={"source": "unit"},
        message_channel="platform-test-channel",
        message_thread="thread-1",
    )
    assert task.status == "queued"
    assert task.message_channel == "platform-test-channel"
    assert task.task_metadata == {"source": "unit"}
    # task_created event
    from sqlalchemy import select

    from shared.lib.models import TaskEvent

    events = (await db_session.execute(select(TaskEvent).where(TaskEvent.task_id == task.id))).scalars().all()
    assert any(e.event_type == "task_created" for e in events)


@pytest.mark.asyncio
async def test_threadless_task_uses_workflow_default_message_channel(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.lib.task_queue._workflow_default_message_channel",
        AsyncMock(return_value="workflow-default"),
    )

    task = await create_task(
        db_session,
        workflow="platform-test",
        prompt="connector alert",
        metadata={"source": "connector", "channel_id": "source-channel-id", "team_id": "source-team"},
        message_channel="source-channel",
    )

    assert task.message_channel == "workflow-default"
    assert task.task_metadata == {"source": "connector"}


@pytest.mark.asyncio
async def test_dequeue_task_marks_running_and_sets_heartbeat(db_session) -> None:
    await create_task(db_session, workflow="platform-test", prompt="p")
    task = await dequeue_task(db_session, workflow="platform-test")
    assert task is not None
    assert task.status == "running"
    assert task.heartbeat is not None


@pytest.mark.asyncio
async def test_dequeue_respects_max_running(db_session) -> None:
    # Create two tasks, mark one running, cap max_running=1.
    await create_task(db_session, workflow="platform-test", prompt="1")
    await create_task(db_session, workflow="platform-test", prompt="2")
    first = await dequeue_task(db_session, workflow="platform-test", max_running=1)
    assert first is not None
    second = await dequeue_task(db_session, workflow="platform-test", max_running=1)
    assert second is None, "expected second dequeue to respect max_running cap"


@pytest.mark.asyncio
async def test_dequeue_respects_platform_running_limit(db_session) -> None:
    await create_task(db_session, workflow="workflow-a", prompt="first")
    await create_task(db_session, workflow="workflow-b", prompt="second")

    first = await dequeue_task(
        db_session,
        platform_max_running=1,
        workflow_limits={"workflow-a": 1, "workflow-b": 1},
    )
    second = await dequeue_task(
        db_session,
        platform_max_running=1,
        workflow_limits={"workflow-a": 1, "workflow-b": 1},
    )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_global_dequeue_respects_each_workflow_limit(db_session) -> None:
    await create_task(db_session, workflow="workflow-a", prompt="first")
    await create_task(db_session, workflow="workflow-a", prompt="second")

    first = await dequeue_task(
        db_session,
        platform_max_running=3,
        workflow_limits={"workflow-a": 1},
    )
    second = await dequeue_task(
        db_session,
        platform_max_running=3,
        workflow_limits={"workflow-a": 1},
    )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_global_dequeue_respects_workflow_limit_across_sessions(async_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as setup:
        await create_task(setup, workflow="workflow-a", prompt="first")
        await create_task(setup, workflow="workflow-a", prompt="second")

    kwargs = {
        "platform_max_running": 3,
        "workflow_limits": {"workflow-a": 1},
    }
    async with factory() as first_session:
        first = await dequeue_task(first_session, **kwargs)
    async with factory() as second_session:
        second = await dequeue_task(second_session, **kwargs)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_global_dequeue_skips_capped_workflow_for_next_priority(db_session) -> None:
    await create_task(db_session, workflow="high-workflow", prompt="running")
    await create_task(db_session, workflow="high-workflow", prompt="queued")
    await create_task(db_session, workflow="low-workflow", prompt="queued")
    first = await dequeue_task(
        db_session,
        platform_max_running=3,
        workflow_limits={"high-workflow": 1, "low-workflow": 1},
        workflow_priorities={"high-workflow": 0, "low-workflow": 2},
    )

    assert first is not None
    assert first.workflow == "high-workflow"

    second = await dequeue_task(
        db_session,
        platform_max_running=3,
        workflow_limits={"high-workflow": 1, "low-workflow": 1},
        workflow_priorities={"high-workflow": 0, "low-workflow": 2},
    )

    assert second is not None
    assert second.workflow == "low-workflow"


@pytest.mark.asyncio
async def test_dequeue_prefers_higher_workflow_priority(db_session) -> None:
    low = await create_task(db_session, workflow="low-workflow", prompt="low")
    high = await create_task(db_session, workflow="high-workflow", prompt="high")

    task = await dequeue_task(
        db_session,
        platform_max_running=3,
        workflow_limits={"low-workflow": 1, "high-workflow": 1},
        workflow_priorities={"low-workflow": 2, "high-workflow": 0},
    )

    assert task is not None
    assert task.id == high.id
    assert task.id != low.id


@pytest.mark.asyncio
async def test_dequeue_skips_locked_across_connections(async_engine) -> None:
    """Two concurrent sessions must not receive the same task (SKIP LOCKED)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as setup:
        await create_task(setup, workflow="platform-test", prompt="exactly-one")

    results: list[object] = []

    async def worker():
        async with factory() as s:
            t = await dequeue_task(s, workflow="platform-test")
            results.append(t.id if t else None)

    await asyncio.gather(worker(), worker())

    assert len(results) == 2
    non_null = [r for r in results if r is not None]
    assert len(non_null) == 1, f"expected exactly one dequeue winner, got {results}"


@pytest.mark.asyncio
async def test_coalesce_merges_into_existing_task(db_session) -> None:
    first = await create_task(
        db_session,
        workflow="platform-test",
        prompt="alert-a",
        coalesce_key="alert-group-1",
        metadata={"alert_id": "A1"},
        coalesce_window_sec=600,
    )
    merged = await create_task(
        db_session,
        workflow="platform-test",
        prompt="alert-b",
        coalesce_key="alert-group-1",
        metadata={"alert_id": "A2"},
        coalesce_window_sec=600,
    )
    assert merged.id == first.id, "second create must merge into the first"
    alerts = merged.task_metadata.get("coalesced_alerts")
    assert alerts and alerts[0]["alert_id"] == "A2"


@pytest.mark.asyncio
async def test_complete_task_and_heartbeat(db_session) -> None:
    t = await create_task(db_session, workflow="platform-test", prompt="x")
    await dequeue_task(db_session, workflow="platform-test")
    await heartbeat(db_session, t.id)
    await complete_task(db_session, t.id, status="succeeded", tokens_used=42, duration_sec=1.5)
    from shared.lib.models import Task

    refreshed = await db_session.get(Task, t.id)
    assert refreshed.status == "succeeded"
    assert refreshed.tokens_used == 42
