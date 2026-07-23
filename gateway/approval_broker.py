"""Gateway-owned approval broker helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.approvals import apply_approval_event, find_approval_by_request
from shared.lib.config import settings
from shared.lib.mattermost_api import MattermostAPIError, create_post
from shared.lib.models import Approval, SessionEvent, Task

logger = logging.getLogger(__name__)


def approval_callback_url() -> str:
    explicit = settings.gateway_public_base_url.strip().rstrip("/")
    if explicit:
        return f"{explicit}/webhooks/message/actions/approval"
    return f"http://gateway:{settings.gateway_port}/webhooks/message/actions/approval"


def _session_details_url(task_id: uuid.UUID) -> str | None:
    base = settings.control_plane_ui_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/sessions/{task_id}"


def _approval_action_context(approval: Approval, *, decision: str) -> dict[str, Any]:
    approval_requested = (approval.approval_metadata or {}).get("approval_requested", {})
    return {
        "approval_id": str(approval.id),
        "task_id": str(approval.task_id),
        "tool_name": approval.tool_name,
        "request_id": str(approval_requested.get("request_id") or ""),
        "decision": decision,
        "token": settings.message_outgoing_webhook_secret,
    }


def _approval_post_payload(task: Task, approval: Approval) -> tuple[str, dict[str, Any]]:
    approval_requested = (approval.approval_metadata or {}).get("approval_requested", {})
    issue = approval_requested.get("task_prompt_summary") or (task.prompt[:300] if task.prompt else "")
    action_desc = approval.request_preview or approval.tool_name

    lines = [
        f":warning: **Approval Required** (Task `{str(task.id)[:8]}`)",
        "",
        f"**Tool**: `{approval.tool_name}`",
    ]
    if action_desc:
        lines.extend(["", "**Request Preview**:", f"```\n{action_desc[:1200]}\n```"])
    if approval.workflow:
        lines.append(f"**Workflow**: `{approval.workflow}`")
    if issue:
        lines.append(f"**Issue**: {issue}")
    session_url = _session_details_url(task.id)
    if session_url:
        lines.append(f"**Session**: [Open details]({session_url})")
    lines.extend(["", "Use the buttons below to approve or reject this action."])

    props = {
        "attachments": [
            {
                "text": "Approve or reject this tool execution.",
                "actions": [
                    {
                        "id": "approve",
                        "type": "button",
                        "name": "Approve",
                        "style": "success",
                        "integration": {
                            "url": approval_callback_url(),
                            "context": _approval_action_context(approval, decision="approve"),
                        },
                    },
                    {
                        "id": "reject",
                        "type": "button",
                        "name": "Reject",
                        "style": "danger",
                        "integration": {
                            "url": approval_callback_url(),
                            "context": _approval_action_context(approval, decision="reject"),
                        },
                    },
                ],
            }
        ]
    }
    return "\n".join(lines), props


async def record_approval_result(
    session: AsyncSession,
    task: Task,
    approval: Approval,
    *,
    approved: bool,
    reason: str | None = None,
    approved_by: str | None = None,
    approved_by_user_id: str | None = None,
    approval_reply: str | None = None,
    source: str = "gateway",
) -> Approval:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "tool_name": approval.tool_name,
        "approved": approved,
        "source": source,
    }
    if reason:
        payload["reason"] = reason
    if approved_by:
        payload["approved_by"] = approved_by
    if approved_by_user_id:
        payload["approved_by_user_id"] = approved_by_user_id
    if approval_reply:
        payload["approval_reply"] = approval_reply

    session.add(
        SessionEvent(
            task_id=task.id,
            event_type="approval_result",
            timestamp=now,
            data=payload,
        )
    )
    resolved = await apply_approval_event(session, task, task.id, "approval_result", now, payload)
    if task.status == "waiting_approval":
        session.add(
            SessionEvent(
                task_id=task.id,
                event_type="approval_wait_resolved",
                timestamp=now,
                data={"tool_name": approval.tool_name, "approved": approved, "source": source},
            )
        )
    assert resolved is not None
    return resolved


async def ensure_approval_prompt_posted(
    session: AsyncSession,
    task: Task | None,
    approval: Approval | None,
) -> Approval | None:
    if task is None or approval is None:
        return approval
    if approval.status != "pending":
        return approval

    metadata = approval.approval_metadata or {}
    gateway_delivery = metadata.get("gateway_delivery") or {}
    if gateway_delivery.get("post_id"):
        return approval

    await session.flush()

    if not (task.message_channel or task.message_thread or task.task_metadata.get("channel_id")):
        metadata["gateway_delivery"] = {
            "delivery_failed_at": datetime.now(UTC).isoformat(),
            "error": "Approval channel unavailable for this task",
        }
        approval.approval_metadata = metadata
        approval.updated_at = datetime.now(UTC)
        return approval

    text, props = _approval_post_payload(task, approval)
    channel_id = str(task.task_metadata.get("channel_id") or "")
    team_id = str(task.task_metadata.get("team_id") or "")
    team_name = str(task.task_metadata.get("team_domain") or settings.message_bus_team_name or "")

    # Interactive approval prompts use Mattermost message `props` (buttons), which
    # are provider-specific UI. This path stays on the Mattermost adapter directly
    # until an equivalent Slack interactivity/ingress integration exists; plain
    # notifications go through the provider-neutral message bus instead.
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            post = await create_post(
                client,
                api_url=settings.message_bus_api_url,
                bot_token=settings.message_bus_bot_token,
                text=text,
                channel_id=channel_id,
                channel_name=task.message_channel or "",
                team_id=team_id,
                team_name=team_name,
                root_id=task.message_thread or "",
                props=props,
            )
        except (MattermostAPIError, httpx.HTTPError) as exc:
            logger.warning("Failed to post approval prompt for %s: %s", approval.tool_name, exc)
            metadata = approval.approval_metadata or {}
            metadata["gateway_delivery"] = {
                "delivery_failed_at": datetime.now(UTC).isoformat(),
                "error": str(exc),
            }
            approval.approval_metadata = metadata
            approval.updated_at = datetime.now(UTC)
            return approval

    metadata = approval.approval_metadata or {}
    metadata["gateway_delivery"] = {
        "post_id": str(post.get("id") or ""),
        "root_id": str(post.get("root_id") or ""),
        "channel_id": str(post.get("channel_id") or channel_id),
        "posted_at": datetime.now(UTC).isoformat(),
    }
    approval.approval_metadata = metadata
    approval.updated_at = datetime.now(UTC)
    return approval


async def get_runtime_approval(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    tool_name: str,
    request_id: str,
) -> Approval | None:
    return await find_approval_by_request(session, task_id, tool_name, request_id)
