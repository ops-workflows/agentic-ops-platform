"""Generic Message MCP Server for visible human communication and workflow handoffs."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, extract_bearer_token, get_env
from shared.lib.message_bus import build_message_bus

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

MESSAGE_BUS_PROVIDER = get_env("MESSAGE_BUS_PROVIDER", "mattermost").strip().lower() or "mattermost"
MESSAGE_BUS_API_URL = get_env("MESSAGE_BUS_API_URL", "")
GATEWAY_URL = get_env("GATEWAY_URL", "http://gateway:8080")

mcp = FastMCP("Message MCP Server")


def _header(headers: dict[str, str], name: str) -> str:
    return headers.get(name.lower(), "")


def _resolve_channel(channel: str | None, headers: dict[str, str]) -> str:
    return channel or _header(headers, "x-message-channel")


def _resolve_channel_id(channel_id: str | None, headers: dict[str, str]) -> str:
    return channel_id or _header(headers, "x-message-channel-id")


def _resolve_thread(thread_id: str | None, headers: dict[str, str]) -> str:
    return thread_id or _header(headers, "x-message-thread-id")


def _resolve_team_id(headers: dict[str, str]) -> str:
    return _header(headers, "x-message-team-id")


def _resolve_team_name(headers: dict[str, str]) -> str:
    return _header(headers, "x-message-team-name")


def _append_task_footer(text: str, headers: dict[str, str]) -> str:
    task_id = _header(headers, "x-task-id")
    if task_id:
        return f"{text}\n\n_Task: `{task_id[:8]}`_"
    return text


async def _post_message_async(
    *,
    text: str,
    bot_token: str,
    channel: str,
    channel_id: str,
    thread_id: str,
    team_id: str,
    team_name: str,
) -> dict[str, Any]:
    if MESSAGE_BUS_PROVIDER not in {"mattermost", "slack"}:
        return {"error": f"Unsupported MESSAGE_BUS_PROVIDER: {MESSAGE_BUS_PROVIDER}"}
    if not bot_token:
        return {"error": "Authorization header required"}
    if not channel and not channel_id:
        return {"error": "No channel or channel_id specified and session headers are missing"}

    active_thread_id = thread_id

    async with httpx.AsyncClient(timeout=10.0) as client:

        async def client_factory() -> httpx.AsyncClient:
            return client

        def get_thread_id() -> str:
            return active_thread_id

        def set_thread_id(value: str) -> None:
            nonlocal active_thread_id
            active_thread_id = value

        bus = build_message_bus(
            provider=MESSAGE_BUS_PROVIDER,
            client_factory=client_factory,
            api_url=MESSAGE_BUS_API_URL,
            bot_token=bot_token,
            channel_id=channel_id,
            channel_name=channel,
            team_id=team_id,
            team_name=team_name,
            get_thread_id=get_thread_id,
            set_thread_id=set_thread_id,
        )
        posted = await bus.post_to_thread(text)

    if not posted:
        detail = str(getattr(bus, "last_error", "") or "").strip()
        return {"error": detail or "Message provider did not return a post"}
    return {"success": True, "post_id": posted.id, "thread_id": posted.thread_id}


def _post_message(
    *,
    text: str,
    bot_token: str,
    channel: str,
    channel_id: str,
    thread_id: str,
    team_id: str,
    team_name: str,
) -> dict[str, Any]:
    try:
        return asyncio.run(
            _post_message_async(
                text=text,
                bot_token=bot_token,
                channel=channel,
                channel_id=channel_id,
                thread_id=thread_id,
                team_id=team_id,
                team_name=team_name,
            )
        )
    except httpx.HTTPError as exc:
        logger.error("Message provider API failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(annotations={"openWorldHint": True})
def post_message(
    text: Annotated[str, "Message text to post. Markdown is supported."],
    channel: Annotated[str | None, "Optional channel name override. Defaults to the current task channel."] = None,
    channel_id: Annotated[str | None, "Optional channel ID override. Defaults to the current task channel ID."] = None,
    thread_id: Annotated[str | None, "Optional thread root override. Defaults to the current task thread."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this for visible notes that are separate from the workflow's final result text."""
    return _post_message(
        text=_append_task_footer(text, headers),
        bot_token=extract_bearer_token(headers),
        channel=_resolve_channel(channel, headers),
        channel_id=_resolve_channel_id(channel_id, headers),
        thread_id=_resolve_thread(thread_id, headers),
        team_id=_resolve_team_id(headers),
        team_name=_resolve_team_name(headers),
    )


@mcp.tool(annotations={"openWorldHint": True})
def handoff_task(
    workflow: Annotated[
        str,
        "Different target workflow that owns the follow-up task; never use this for an in-session specialist.",
    ],
    prompt: Annotated[str, "Self-contained follow-up work to enqueue for that different workflow."],
    text: Annotated[str, "Visible cross-workflow assignment note to post before creating the follow-up task."],
    channel: Annotated[str | None, "Optional channel name override. Defaults to the current task channel."] = None,
    channel_id: Annotated[str | None, "Optional channel ID override. Defaults to the current task channel ID."] = None,
    thread_id: Annotated[str | None, "Optional thread override. Defaults to the current task thread."] = None,
    metadata: Annotated[dict[str, Any] | None, "Optional structured metadata to attach to the created task."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Assign follow-up work to another workflow, with a visible post and queued task.

    Do not use this to delegate to, message, wait for, or retrieve a Claude Code
    subagent in the current investigation. Use the built-in Agent and SendMessage
    tools for internal specialist work.
    """
    resolved_channel = _resolve_channel(channel, headers)
    resolved_channel_id = _resolve_channel_id(channel_id, headers)
    resolved_thread = _resolve_thread(thread_id, headers)
    posted = _post_message(
        text=_append_task_footer(text, headers),
        bot_token=extract_bearer_token(headers),
        channel=resolved_channel,
        channel_id=resolved_channel_id,
        thread_id=resolved_thread,
        team_id=_resolve_team_id(headers),
        team_name=_resolve_team_name(headers),
    )
    if posted.get("error"):
        return posted

    payload_metadata = dict(metadata or {})
    payload_metadata.setdefault("source", "agent-handoff")
    if headers.get("x-task-id"):
        payload_metadata.setdefault("source_task_id", headers["x-task-id"])
    if headers.get("x-task-workflow"):
        payload_metadata.setdefault("source_workflow", headers["x-task-workflow"])
    payload_metadata.setdefault("handoff_post_id", posted.get("post_id", ""))

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{GATEWAY_URL}/tasks",
            json={
                "workflow": workflow,
                "prompt": prompt,
                "channel": "message",
                "metadata": payload_metadata,
                "message_channel": resolved_channel,
                "message_thread": resolved_thread or None,
            },
        )
        response.raise_for_status()
        created_task = response.json()

    return {"success": True, "task_id": created_task.get("id", ""), "post_id": posted.get("post_id", "")}


@mcp.tool(annotations={"openWorldHint": True})
def post_rca_summary(
    case_id: Annotated[str, "Case or incident identifier."],
    root_cause: Annotated[str, "Root cause summary to post."],
    problem_summary: Annotated[str | None, "Optional original problem summary."] = None,
    customer: Annotated[str | None, "Optional customer name or identifier."] = None,
    priority: Annotated[str | None, "Optional priority such as P1 or P2."] = None,
    service: Annotated[str | None, "Optional affected service name."] = None,
    similar_incidents: Annotated[str | None, "Optional similar incident notes."] = None,
    recommended_actions: Annotated[str | None, "Optional recommended actions block."] = None,
    confidence: Annotated[str | None, "Optional confidence label."] = None,
    cost: Annotated[str | None, "Optional estimated analysis cost."] = None,
    duration: Annotated[str | None, "Optional analysis duration."] = None,
    channel: Annotated[str | None, "Optional channel name override."] = None,
    channel_id: Annotated[str | None, "Optional channel ID override."] = None,
    thread_id: Annotated[str | None, "Optional thread root post ID override."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this when you need a structured RCA format instead of a plain text post."""
    parts = [f"## Incident Analysis: {case_id}", ""]
    if problem_summary:
        parts.extend(
            [
                "**Problem Summary**",
                problem_summary,
                f"Customer: {customer or 'N/A'} | Priority: {priority or 'N/A'} | Service: {service or 'N/A'}",
                "",
            ]
        )
    parts.extend(["**Root Cause**", root_cause, ""])
    if similar_incidents:
        parts.extend(["**Similar Past Incidents**", similar_incidents, ""])
    if recommended_actions:
        parts.extend(["**Recommended Actions**", recommended_actions, ""])
    footer_parts = []
    if confidence:
        footer_parts.append(f"**Confidence**: {confidence}")
    if cost:
        footer_parts.append(f"**Cost**: {cost}")
    if duration:
        footer_parts.append(f"**Duration**: {duration}")
    if footer_parts:
        parts.append(" | ".join(footer_parts))
    return post_message(
        "\n".join(parts),
        channel=channel,
        channel_id=channel_id,
        thread_id=thread_id,
        headers=headers,
    )


@mcp.tool(annotations={"openWorldHint": True})
def ask_approval(
    question: Annotated[str, "What you need approval for."],
    options: Annotated[list[str], "Decision options such as Approve or Reject."],
    context: Annotated[str | None, "Optional supporting context for the decision."] = None,
    channel: Annotated[str | None, "Optional channel name override."] = None,
    channel_id: Annotated[str | None, "Optional channel ID override."] = None,
    thread_id: Annotated[str | None, "Optional thread root post ID override."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this only when the harness approval gate is not the right mechanism."""
    options_text = "\n".join(f"  - **{option}**" for option in options)
    message = (
        f"**Approval Required**\n\n{question}\n\n"
        f"**Options:**\n{options_text}\n\n_Reply in this thread with your choice._"
    )
    if context:
        message = (
            f"**Approval Required**\n\n{question}\n\n**Options:**\n{options_text}\n\n"
            f"*Context:* {context}\n\n_Reply in this thread with your choice._"
        )
    return post_message(message, channel=channel, channel_id=channel_id, thread_id=thread_id, headers=headers)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-message", "provider": MESSAGE_BUS_PROVIDER})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
