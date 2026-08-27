"""Shared Slack Web API helpers for provider-neutral message features."""

from __future__ import annotations

import re
from typing import Any

import httpx


class SlackAPIError(Exception):
    """Raised when Slack rejects a Web API request."""


_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{5,}$", re.IGNORECASE)
_MAX_THREAD_FETCH_PAGES = 50


def _headers(bot_token: str) -> dict[str, str]:
    if not bot_token:
        raise SlackAPIError("Slack bot token is required")
    return {"Authorization": f"Bearer {bot_token}"}


def _api_base_url(api_url: str) -> str:
    normalized = api_url.strip().rstrip("/")
    if not normalized:
        raise SlackAPIError("Slack API URL is required")
    return normalized


async def _call(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    method: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        f"{_api_base_url(api_url)}/{method}",
        json=body,
        headers=_headers(bot_token),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise SlackAPIError(str(payload.get("error") or f"Slack {method} failed"))
    return payload


async def create_post(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    channel_id: str,
    text: str,
    thread_id: str = "",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"channel": channel_id, "text": text}
    if thread_id:
        body["thread_ts"] = thread_id
    if blocks is not None:
        body["blocks"] = blocks
    return await _call(
        client,
        api_url=api_url,
        bot_token=bot_token,
        method="chat.postMessage",
        body=body,
    )


async def update_post(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    channel_id: str,
    post_id: str,
    text: str,
) -> dict[str, Any]:
    return await _call(
        client,
        api_url=api_url,
        bot_token=bot_token,
        method="chat.update",
        body={"channel": channel_id, "ts": post_id, "text": text, "blocks": []},
    )


async def get_authenticated_user_id(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
) -> str:
    payload = await _call(
        client,
        api_url=api_url,
        bot_token=bot_token,
        method="auth.test",
        body={},
    )
    return str(payload.get("user_id") or "")


async def fetch_thread_messages(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    channel_id: str,
    thread_id: str,
    current_message_id: str,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Return the latest Slack thread messages through the current reply."""
    messages: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    page_count = 0
    while True:
        page_count += 1
        if page_count > _MAX_THREAD_FETCH_PAGES:
            raise SlackAPIError("Slack thread exceeded the pagination safety limit")
        body: dict[str, Any] = {
            "channel": channel_id,
            "ts": thread_id,
            "latest": current_message_id,
            "inclusive": True,
            "limit": 100,
        }
        if cursor:
            body["cursor"] = cursor
        payload = await _call(
            client,
            api_url=api_url,
            bot_token=bot_token,
            method="conversations.replies",
            body=body,
        )
        page = payload.get("messages")
        if isinstance(page, list):
            messages.extend(message for message in page if isinstance(message, dict))
        metadata = payload.get("response_metadata")
        next_cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise SlackAPIError("Slack thread returned a repeated pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    ordered_messages = sorted(messages, key=lambda message: str(message.get("ts") or ""))[-limit:]
    return [
        {
            "message_id": str(message.get("ts") or ""),
            "author": str(message.get("user") or message.get("username") or message.get("bot_id") or "unknown"),
            "text": str(message.get("text") or "").strip(),
        }
        for message in ordered_messages
        if str(message.get("text") or "").strip()
    ]


async def resolve_channel_id(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    channel_id: str = "",
    channel_name: str = "",
) -> str:
    """Resolve a Slack channel name to an ID visible to the configured bot."""
    if channel_id:
        return channel_id

    normalized_name = channel_name.strip().lstrip("#")
    if not normalized_name:
        raise SlackAPIError("Slack channel_id or channel name is required")
    if _CHANNEL_ID_RE.fullmatch(normalized_name):
        return normalized_name

    cursor = ""
    while True:
        params: dict[str, str | int] = {
            "exclude_archived": "true",
            "limit": 200,
            "types": "public_channel,private_channel",
        }
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            f"{_api_base_url(api_url)}/conversations.list",
            params=params,
            headers=_headers(bot_token),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise SlackAPIError(str(payload.get("error") or "Slack conversations.list failed"))
        for channel in payload.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if str(channel.get("name") or "").strip().lower() == normalized_name.lower():
                return str(channel.get("id") or "")
        metadata = payload.get("response_metadata")
        cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
        if not cursor:
            break

    raise SlackAPIError(f"Slack channel '{normalized_name}' was not found for the configured bot token")
