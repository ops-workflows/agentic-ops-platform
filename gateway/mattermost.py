"""Message webhook handler for human → agent ingress.

The first inbound provider is Mattermost outgoing webhooks. Each configured
message channel maps 1:1 to a workflow via `agent.yaml` → `messaging.channels`.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from gateway.approval_broker import get_runtime_approval, record_approval_result
from gateway.plugin_dir import discover_all_message_routes
from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.models import Approval, SessionEvent, Task
from shared.lib.task_queue import create_task

logger = logging.getLogger(__name__)
router = APIRouter()


class MattermostOutgoingWebhook(BaseModel):
    """Mattermost outgoing webhook payload."""

    token: str = ""
    team_id: str = ""
    team_domain: str = ""
    channel_id: str = ""
    channel_name: str = ""
    timestamp: int = 0
    user_id: str = ""
    user_name: str = ""
    post_id: str = ""
    text: str = ""
    trigger_word: str = ""
    file_ids: str = ""


class MattermostInteractiveAction(BaseModel):
    user_id: str = ""
    post_id: str = ""
    channel_id: str = ""
    team_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


def _verify_webhook_token(token: str) -> bool:
    """Verify the outgoing webhook token matches our configured secret."""
    if not settings.message_outgoing_webhook_secret:
        logger.warning("MESSAGE_OUTGOING_WEBHOOK_SECRET not set; skipping verification")
        return True
    return hmac.compare_digest(token, settings.message_outgoing_webhook_secret)


def _verify_interactive_action_token(token: str) -> bool:
    if not settings.message_outgoing_webhook_secret:
        logger.warning("MESSAGE_OUTGOING_WEBHOOK_SECRET not set; skipping interactive verification")
        return True
    return hmac.compare_digest(token, settings.message_outgoing_webhook_secret)


def _load_trigger_routes() -> dict[str, str]:
    """Load message channel -> workflow mappings from workflow configs."""
    return discover_all_message_routes()


def _strip_trigger_word(text: str, trigger_word: str, trigger_routes: dict[str, str]) -> str:
    """Strip the @agent trigger word from message text, returning the raw user message."""
    cleaned = text.strip()
    # Strip common trigger prefix
    for prefix in ["@agent"]:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    # Also strip whatever Mattermost says the trigger word was
    if trigger_word and cleaned.lower().startswith(trigger_word.strip().lower()):
        cleaned = cleaned[len(trigger_word.strip()) :].strip()
    return cleaned


def _resolve_workflow(channel_name: str, trigger_routes: dict[str, str]) -> str | None:
    """Resolve the workflow name for a webhook request based on message channel."""
    normalized_channel = channel_name.strip().lower()
    return trigger_routes.get(normalized_channel)


def _detect_gateway_shortcut(text: str) -> str | None:
    """Detect Gateway-handled shortcuts that don't need an agent session.

    Returns the shortcut name, or None if this should be routed to an agent.
    """
    lower = text.strip().lower()
    if lower == "status":
        return "status"
    if lower in ("help", "?"):
        return "help"
    return None


def _comment_response(text: str) -> dict[str, str]:
    """Return an outgoing webhook response as a thread comment."""
    return {
        "response_type": "comment",
        "text": text,
    }


def _interactive_error(message: str) -> dict[str, Any]:
    return {"error": {"message": message}}


def _render_help_text(trigger_routes: dict[str, str]) -> str:
    """Render a help message from configured channel-workflow mappings."""
    if not trigger_routes:
        return "No message workflow channels are configured."

    lines = [
        "**Available workflow channels**",
        "",
    ]

    for channel, workflow in sorted(trigger_routes.items()):
        lines.append(f"- `#{channel}` → `{workflow}`")

    lines.extend(
        [
            "",
            "Use `@agent help` in any configured channel to see this list.",
            "Use `@agent status` to see recent tasks.",
        ]
    )
    return "\n".join(lines)


async def _render_status_text() -> str:
    """Query Postgres tasks and format a status table."""
    from shared.lib.task_queue import list_tasks

    async with async_session_factory() as session:
        tasks = await list_tasks(session, limit=10)

    if not tasks:
        return "No recent tasks found."

    lines = ["| Status | Workflow | Created | Duration |", "|---|---|---|---|"]
    for t in tasks:
        dur = f"{t.duration_sec:.1f}s" if t.duration_sec else "—"
        lines.append(f"| {t.status} | {t.workflow} | {t.created.strftime('%H:%M')} | {dur} |")

    return "\n".join(lines)


@router.post("/message")
async def message_webhook(request: Request):
    """Handle Mattermost outgoing webhook.

    Gateway-handled shortcuts (status, help) and the initial task ack are
    returned synchronously in the outgoing webhook response.
    Everything else is passed as raw text to the agent — the agent's
    CLAUDE.md and skills handle routing, formatting, and off-topic rejection.

    Mattermost outgoing webhooks may send application/json or
    application/x-www-form-urlencoded depending on configuration.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        # Mattermost default: application/x-www-form-urlencoded
        form = await request.form()
        body = dict(form.items())
    webhook = MattermostOutgoingWebhook(**body)

    # Verify webhook token
    if not _verify_webhook_token(webhook.token):
        logger.warning("Invalid webhook token from %s", webhook.user_name)
        raise HTTPException(status_code=403, detail="Invalid Mattermost webhook token")

    trigger_routes = _load_trigger_routes()
    workflow = _resolve_workflow(webhook.channel_name, trigger_routes)
    raw_message = _strip_trigger_word(webhook.text, webhook.trigger_word, trigger_routes)
    logger.info("MM webhook: user=%s message=%s", webhook.user_name, raw_message[:100])

    # Handle Gateway shortcuts (no agent session needed)
    shortcut = _detect_gateway_shortcut(raw_message)

    if shortcut == "help":
        return _comment_response(_render_help_text(trigger_routes))

    if shortcut == "status":
        return _comment_response(await _render_status_text())

    if not raw_message:
        return _comment_response(_render_help_text(trigger_routes))

    if not workflow:
        return _comment_response(_render_help_text(trigger_routes))

    # Create task — raw message becomes the prompt, agent handles everything else
    async with async_session_factory() as session:
        await create_task(
            session,
            workflow=workflow,
            prompt=raw_message,
            channel="mattermost",
            metadata={
                "source": "mattermost",
                "user": webhook.user_name,
                "channel_id": webhook.channel_id,
                "channel": webhook.channel_name,
                "team_id": webhook.team_id,
                "team_domain": webhook.team_domain,
            },
            message_channel=webhook.channel_name,
            message_thread=webhook.post_id,
        )

    return _comment_response(":brain: Working on it...")


@router.post("/message/actions/approval")
async def message_approval_action(payload: MattermostInteractiveAction):
    context = payload.context or {}
    if not _verify_interactive_action_token(str(context.get("token") or "")):
        return _interactive_error("Invalid approval action token")

    decision = str(context.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return _interactive_error("Unsupported approval action")

    try:
        approval_id = UUID(str(context.get("approval_id") or ""))
    except ValueError:
        return _interactive_error("Invalid approval identifier")

    async with async_session_factory() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            return _interactive_error("Approval not found")

        task = await session.get(Task, approval.task_id)
        if task is None:
            return _interactive_error("Associated task not found")

        request_id = str(context.get("request_id") or "")
        if request_id:
            matched = await get_runtime_approval(
                session,
                task_id=approval.task_id,
                tool_name=approval.tool_name,
                request_id=request_id,
            )
            if matched is None or matched.id != approval.id:
                return _interactive_error("Approval action no longer matches an open request")

        if approval.status != "pending":
            return {
                "update": {
                    "message": f":lock: Approval already resolved as **{approval.status}**.",
                    "props": {},
                },
                "ephemeral_text": f"This approval is already {approval.status}.",
                "skip_slack_parsing": True,
            }

        approved = decision == "approve"
        session.add(
            SessionEvent(
                task_id=task.id,
                event_type="approval_action",
                data={
                    "approval_id": str(approval.id),
                    "tool_name": approval.tool_name,
                    "decision": decision,
                    "user_id": payload.user_id,
                    "post_id": payload.post_id,
                    "channel_id": payload.channel_id,
                },
            )
        )
        await record_approval_result(
            session,
            task,
            approval,
            approved=approved,
            reason=None if approved else f"Approval rejected in message provider by {payload.user_id or 'operator'}",
            approved_by=payload.user_id or None,
            approved_by_user_id=payload.user_id or None,
            approval_reply=decision,
            source="message_interactive",
        )
        await session.commit()

    status_label = "approved" if approved else "rejected"
    approval_message = (
        f":white_check_mark: Approval **{status_label}** for "
        f"`{approval.tool_name}` by `{payload.user_id or 'operator'}`."
    )
    return {
        "update": {
            "message": approval_message,
            "props": {},
        },
        "ephemeral_text": f"You {status_label} this approval request.",
        "skip_slack_parsing": True,
    }
