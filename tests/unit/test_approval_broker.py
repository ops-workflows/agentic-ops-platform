"""Unit coverage for gateway-owned approval delivery behavior."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from gateway import approval_broker
from shared.lib.mattermost_api import MattermostAPIError

pytestmark = pytest.mark.unit


class _Session:
    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_approval_delivery_keeps_request_pending(monkeypatch) -> None:
    task = SimpleNamespace(
        id=uuid.uuid4(),
        workflow="platform-test",
        prompt="Investigate this failing command.",
        message_channel="platform-test-channel",
        message_thread=None,
        task_metadata={},
    )
    approval = SimpleNamespace(
        id=uuid.uuid4(),
        task_id=task.id,
        workflow=task.workflow,
        approval_kind="operator_approval",
        tool_name="Bash",
        status="pending",
        request_preview="echo approval-needed",
        approval_metadata={},
        updated_at=None,
    )

    async def failed_create_post(*_args, **_kwargs):
        raise MattermostAPIError("Mattermost is unavailable")

    monkeypatch.setattr(approval_broker, "create_post", failed_create_post)

    updated = await approval_broker.ensure_approval_prompt_posted(_Session(), task, approval)

    assert updated is approval
    assert approval.status == "pending"
    assert approval.approval_metadata["gateway_delivery"]["error"] == "Mattermost is unavailable"


def test_slack_approval_payload_has_provider_action_context() -> None:
    task = SimpleNamespace(id=uuid.uuid4(), prompt="Investigate", workflow="platform-test")
    approval = SimpleNamespace(
        id=uuid.uuid4(),
        task_id=task.id,
        workflow=task.workflow,
        tool_name="Bash",
        request_preview="echo approval-needed",
        approval_metadata={"approval_requested": {"request_id": "request-1"}},
    )

    _text, blocks = approval_broker._slack_approval_post_payload(task, approval)

    actions = blocks[1]["elements"]
    assert {json.loads(action["value"])["decision"] for action in actions} == {"approve", "reject"}
    assert all("token" not in json.loads(action["value"]) for action in actions)
