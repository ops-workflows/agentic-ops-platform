"""Layer 1 — concurrent approvals + AskUserQuestion telemetry.

Exercises ``apply_approval_event`` across the full event lifecycle:
- Multiple simultaneous approvals for the SAME task but DIFFERENT tools
  should each get their own Approval row without cross-talk.
- Re-requesting after resolution creates a new pending approval.
- The ordering rule ("latest pending approval wins") is enforced by
  ``_find_open_approval``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from shared.lib.approvals import _find_open_approval, apply_approval_event
from shared.lib.models import Approval, Task

pytestmark = pytest.mark.service


async def _make_task(db_session, workflow="platform-test") -> Task:
    task = Task(
        id=uuid.uuid4(),
        workflow=workflow,
        prompt="p",
        message_channel="platform-test-channel",
        status="running",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_concurrent_approvals_for_different_tools(async_engine, db_session):
    task = await _make_task(db_session)
    ts = datetime.now(UTC)

    # Two simultaneous approval requests for different tools
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts,
        {"tool_name": "Bash", "tool_input_preview": "echo a"},
    )
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts + timedelta(milliseconds=10),
        {"tool_name": "Write", "tool_input_preview": "./out.txt"},
    )
    await db_session.commit()

    approvals = (
        (await db_session.execute(select(Approval).where(Approval.task_id == task.id).order_by(Approval.requested_at)))
        .scalars()
        .all()
    )
    assert len(approvals) == 2
    tools = {a.tool_name for a in approvals}
    assert tools == {"Bash", "Write"}
    assert all(a.status == "pending" for a in approvals)


@pytest.mark.asyncio
async def test_approval_result_updates_matching_tool_only(async_engine, db_session):
    task = await _make_task(db_session)
    ts = datetime.now(UTC)

    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts,
        {"tool_name": "Bash", "tool_input_preview": "x"},
    )
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts + timedelta(milliseconds=10),
        {"tool_name": "Write", "tool_input_preview": "y"},
    )
    await db_session.commit()

    # Approve only the Bash one
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_result",
        ts + timedelta(seconds=1),
        {"tool_name": "Bash", "approved": True, "reason": "ok"},
    )
    await db_session.commit()

    approvals = (await db_session.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
    by_tool = {a.tool_name: a for a in approvals}
    assert by_tool["Bash"].status == "approved"
    assert by_tool["Write"].status == "pending"


@pytest.mark.asyncio
async def test_find_open_approval_returns_latest_pending(async_engine, db_session):
    task = await _make_task(db_session)
    ts = datetime.now(UTC)

    # First approval: resolved
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts,
        {"tool_name": "Bash", "tool_input_preview": "first"},
    )
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_result",
        ts + timedelta(seconds=1),
        {"tool_name": "Bash", "approved": True},
    )
    # Second approval: pending
    await apply_approval_event(
        db_session,
        task,
        task.id,
        "approval_requested",
        ts + timedelta(seconds=2),
        {"tool_name": "Bash", "tool_input_preview": "second"},
    )
    await db_session.commit()

    open_approval = await _find_open_approval(db_session, task.id, "Bash")
    assert open_approval is not None
    assert open_approval.status == "pending"
    assert open_approval.request_preview == "second"


@pytest.mark.asyncio
async def test_permission_callback_event_creates_pending_approval(async_engine, db_session):
    task = await _make_task(db_session)
    ts = datetime.now(UTC)

    await apply_approval_event(
        db_session,
        task,
        task.id,
        "permission_callback",
        ts,
        {"kind": "operator_approval", "tool_name": "Bash"},
    )
    await db_session.commit()

    approvals = (await db_session.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].approval_kind == "operator_approval"


@pytest.mark.asyncio
async def test_permission_callback_non_operator_kind_is_ignored(async_engine, db_session):
    task = await _make_task(db_session)
    ts = datetime.now(UTC)

    # e.g., AskUserQuestion events arrive via permission_callback too, but
    # with a different kind — they should not create approval rows.
    result = await apply_approval_event(
        db_session,
        task,
        task.id,
        "permission_callback",
        ts,
        {"kind": "ask_user_question", "tool_name": "AskUserQuestion"},
    )
    assert result is None
    await db_session.commit()

    approvals = (await db_session.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
    assert approvals == []
