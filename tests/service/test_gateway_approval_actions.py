from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from gateway.api import get_runtime_approval_status
from gateway.event_collector import EventPayload, receive_event
from gateway.message import MattermostInteractiveAction, message_approval_action
from shared.lib.config import settings
from shared.lib.models import Task
from tests.conftest import run_app_in_background
from tests.fakes.message import FakeMattermost

pytestmark = pytest.mark.service


def _ts() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_gateway_owned_approval_request_and_callback_resolution(db_session) -> None:
    fake_mattermost = FakeMattermost()
    server = run_app_in_background(fake_mattermost.app)

    original_message_bus_api_url = settings.message_bus_api_url
    original_message_bus_bot_token = settings.message_bus_bot_token
    original_message_bus_team_name = settings.message_bus_team_name
    original_gateway_public_base_url = settings.gateway_public_base_url
    original_message_outgoing_webhook_secret = settings.message_outgoing_webhook_secret

    settings.message_bus_api_url = server.base_url
    settings.message_bus_bot_token = "test-bot-token"
    settings.message_bus_team_name = "test-team"
    settings.gateway_public_base_url = "http://127.0.0.1:8080"
    settings.message_outgoing_webhook_secret = "test-shared-secret"

    try:
        task = Task(
            id=uuid.uuid4(),
            workflow="platform-test",
            prompt="Investigate this failing command.",
            status="running",
            message_channel="platform-test-channel",
            message_thread="thread-approval",
            task_metadata={"channel_id": "test-channel-id", "team_id": "test-team-id", "team_domain": "test-team"},
        )
        db_session.add(task)
        await db_session.commit()

        await receive_event(
            EventPayload(
                task_id=str(task.id),
                event_type="approval_requested",
                timestamp=_ts(),
                data={
                    "tool_name": "Bash",
                    "tool_input_preview": "echo approval-needed from service test",
                    "request_id": "req-service-test",
                    "task_prompt_summary": "service test summary",
                },
            )
        )

        posts = fake_mattermost.all_posts()
        assert len(posts) == 1
        post = posts[0]
        actions = post.props.get("attachments", [])[0].get("actions", [])
        approve_action = next(action for action in actions if action.get("id") == "approve")

        action_response = await message_approval_action(
            MattermostInteractiveAction(
                user_id="operator-user",
                post_id=post.id,
                channel_id=post.channel_id,
                team_id="test-team-id",
                context=approve_action.get("integration", {}).get("context", {}),
            )
        )
        assert action_response["ephemeral_text"] == "You approved this approval request."

        status = await get_runtime_approval_status(
            task_id=str(task.id),
            tool_name="Bash",
            request_id="req-service-test",
        )
        assert status.status == "approved"
        assert status.resolved_by_user_id == "operator-user"
    finally:
        settings.message_bus_api_url = original_message_bus_api_url
        settings.message_bus_bot_token = original_message_bus_bot_token
        settings.message_bus_team_name = original_message_bus_team_name
        settings.gateway_public_base_url = original_gateway_public_base_url
        settings.message_outgoing_webhook_secret = original_message_outgoing_webhook_secret
        server.stop()
