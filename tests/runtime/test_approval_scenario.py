"""Layer 2 — approval flow scenarios.

Validates that:
- A tool matching ``permissions.ask`` triggers an approval request in
  Message and the container blocks until a human replies.
- ``approve`` lets the tool execute and the session completes.
- ``reject`` prevents execution and the session still completes.

Requires:
- Docker daemon + built ``ai-ops-agent-runtime:latest``
- ``TEST_RUNTIME_ENABLED=1`` and ``TEST_DATABASE_URL``
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario


async def _click_approval_action(fake_mattermost, gateway_server, *, decision: str, task_id: str) -> None:
    def _wait_for_action():
        post = fake_mattermost.wait_for_post(
            lambda candidate: any(
                action.get("id") == decision
                and str(action.get("integration", {}).get("context", {}).get("task_id") or "") == task_id
                for attachment in (candidate.props or {}).get("attachments", [])
                for action in attachment.get("actions", [])
            ),
            timeout=60.0,
        )
        if post is None:
            return None
        for attachment in (post.props or {}).get("attachments", []):
            for action in attachment.get("actions", []):
                if (
                    action.get("id") == decision
                    and str(action.get("integration", {}).get("context", {}).get("task_id") or "") == task_id
                ):
                    return post, dict(action.get("integration", {}).get("context") or {})
        return None

    match = await asyncio.get_event_loop().run_in_executor(None, _wait_for_action)
    assert match is not None, f"Timed out waiting for {decision} approval button"
    post, context = match
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{gateway_server.base_url}/webhooks/message/actions/approval",
            json={
                "user_id": "operator-user",
                "post_id": post.id,
                "channel_id": post.channel_id,
                "team_id": "test-team-id",
                "context": context,
            },
        )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_approval_approve(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    admit_when_resume_pending,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    _event_collector,
) -> None:
    """Runtime requests approval, operator approves, tool executes."""

    # Turn 1: LLM emits a Bash tool_use matching `ask` pattern
    # ("Bash(echo approval-needed *)")
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {"type": "text", "text": "I need to run a command that requires approval."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo approval-needed test-marker"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Command approved and executed successfully."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Run echo approval-needed something and report the result.",
        message_channel="platform-test-channel",
        message_thread="test-thread-approval",
    )

    click_task = asyncio.create_task(
        _click_approval_action(fake_mattermost, _event_collector, decision="approve", task_id=str(task.id))
    )
    admit_task = asyncio.create_task(admit_when_resume_pending(task.id, workflow=task.workflow, timeout=120))

    exit_code, logs = await spawn_and_wait(task, timeout_sec=60)
    await click_task
    await admit_task

    assert exit_code == 0, f"Container exited with code {exit_code}.\nLogs:\n{logs}"

    # Verify approval-related events were emitted
    approval_events = [e for e in collected_events if e.get("event_type") == "permission_callback"]
    # At minimum, a permission_callback event should have been fired
    # (the exact count depends on runtime behavior)
    assert approval_events, "Expected at least one permission_callback event"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, f"Expected at least 2 LLM turns, got {len(requests)}"

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    from shared.lib.models import TaskEvent

    async with factory() as session:
        event_types = (
            (
                await session.execute(
                    select(TaskEvent.event_type).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created)
                )
            )
            .scalars()
            .all()
        )
    assert "task_resumed" in event_types


@pytest.mark.asyncio
async def test_approval_reject(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    admit_when_resume_pending,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    _event_collector,
) -> None:
    """Runtime requests approval, operator rejects, session completes."""

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {"type": "text", "text": "I need to run a restricted command."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo approval-needed will-be-rejected"},
                    },
                ],
                stop_reason="tool_use",
            ),
            # After rejection, the LLM should get a denial message and conclude
            Turn(
                respond=[{"type": "text", "text": "The command was rejected. Stopping."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Run echo approval-needed something (expect rejection).",
        message_channel="platform-test-channel",
        message_thread="test-thread-reject",
    )

    click_task = asyncio.create_task(
        _click_approval_action(fake_mattermost, _event_collector, decision="reject", task_id=str(task.id))
    )
    admit_task = asyncio.create_task(admit_when_resume_pending(task.id, workflow=task.workflow, timeout=120))

    exit_code, logs = await spawn_and_wait(task, timeout_sec=60)
    await click_task
    await admit_task

    # The container should still exit (the LLM concludes after rejection)
    assert exit_code == 0, f"Container exited with code {exit_code}.\nLogs:\n{logs}"
