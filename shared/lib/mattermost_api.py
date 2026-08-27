"""Shared helpers for posting to Mattermost through the REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_NORMALIZED_MESSAGE_CHARS = 32_000
_ATTACHMENT_TEXT_FIELDS = (
    "fallback",
    "pretext",
    "author_name",
    "title",
    "text",
    "footer",
    "author_link",
    "title_link",
)


class MattermostAPIError(Exception):
    """Raised when Mattermost REST interaction fails."""


def _auth_headers(bot_token: str) -> dict[str, str]:
    if not bot_token:
        raise MattermostAPIError("Mattermost bot token is required")
    return {"Authorization": f"Bearer {bot_token}"}


def _normalize_channel_name(channel_name: str) -> str:
    return channel_name.strip().lstrip("#")


def _append_post_text(values: list[str], value: Any, *, label: str = "") -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if not text:
        return
    rendered = f"{label}: {text}" if label else text
    if rendered not in values:
        values.append(rendered)


def normalize_post_text(post: dict[str, Any]) -> tuple[str, bool]:
    """Return visible Mattermost post and attachment text with a bounded size."""
    values: list[str] = []
    _append_post_text(values, post.get("message"))

    props = post.get("props")
    if isinstance(props, dict):
        attachments = props.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                for field_name in _ATTACHMENT_TEXT_FIELDS:
                    _append_post_text(values, attachment.get(field_name))
                fields = attachment.get("fields")
                if not isinstance(fields, list):
                    continue
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    title = str(field.get("title") or "").strip()
                    _append_post_text(values, field.get("value"), label=title)

    text = "\n".join(values)
    if len(text) <= MAX_NORMALIZED_MESSAGE_CHARS:
        return text, False
    return text[:MAX_NORMALIZED_MESSAGE_CHARS].rstrip(), True


async def fetch_thread_messages(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    root_id: str,
    current_message_id: str,
    current_message_created_at_ms: int,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Return the latest visible Mattermost thread posts through the current reply."""
    response = await client.get(
        f"{api_url.rstrip('/')}/api/v4/posts/{root_id}/thread",
        params={
            "perPage": limit,
            "fromPost": current_message_id,
            "fromCreateAt": current_message_created_at_ms,
            "direction": "up",
        },
        headers=_auth_headers(bot_token),
    )
    response.raise_for_status()
    payload = response.json()
    posts = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(posts, dict):
        raise MattermostAPIError("Mattermost thread response did not contain posts")

    ordered_posts = sorted(
        (post for post in posts.values() if isinstance(post, dict)),
        key=lambda post: (int(post.get("create_at") or 0), str(post.get("id") or "")),
    )[-limit:]
    messages: list[dict[str, str]] = []
    for post in ordered_posts:
        text, _truncated = normalize_post_text(post)
        if not text:
            continue
        props = post.get("props") if isinstance(post.get("props"), dict) else {}
        messages.append(
            {
                "message_id": str(post.get("id") or ""),
                "author": str(
                    props.get("override_username")
                    or props.get("webhook_display_name")
                    or post.get("user_id")
                    or "unknown"
                ),
                "text": text,
            }
        )
    return messages


async def get_authenticated_user_id(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
) -> str:
    """Return the Mattermost user id represented by a bot token."""
    response = await client.get(
        f"{api_url.rstrip('/')}/api/v4/users/me",
        headers=_auth_headers(bot_token),
    )
    response.raise_for_status()
    return str(response.json().get("id") or "")


async def _get_channel_by_team_id(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    headers: dict[str, str],
    team_id: str,
    channel_name: str,
) -> dict[str, Any] | None:
    response = await client.get(
        f"{api_url.rstrip('/')}/api/v4/teams/{team_id}/channels/name/{channel_name}",
        headers=headers,
    )
    if response.status_code in {403, 404}:
        return None
    response.raise_for_status()
    return response.json()


async def _get_channel_by_team_name(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    headers: dict[str, str],
    team_name: str,
    channel_name: str,
) -> dict[str, Any] | None:
    response = await client.get(
        f"{api_url.rstrip('/')}/api/v4/teams/name/{team_name}/channels/name/{channel_name}",
        headers=headers,
    )
    if response.status_code in {403, 404}:
        return None
    response.raise_for_status()
    return response.json()


async def resolve_channel_id(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    channel_id: str = "",
    channel_name: str = "",
    team_id: str = "",
    team_name: str = "",
) -> str:
    """Resolve a Mattermost channel id from explicit or named task context."""
    if channel_id:
        return channel_id

    normalized_channel_name = _normalize_channel_name(channel_name)
    if not normalized_channel_name:
        raise MattermostAPIError("Mattermost channel_id or channel name is required")

    headers = _auth_headers(bot_token)

    if team_id:
        channel = await _get_channel_by_team_id(
            client,
            api_url=api_url,
            headers=headers,
            team_id=team_id,
            channel_name=normalized_channel_name,
        )
        if channel:
            return str(channel["id"])
        raise MattermostAPIError(f"Mattermost channel '{normalized_channel_name}' not found in team id '{team_id}'")

    if team_name:
        channel = await _get_channel_by_team_name(
            client,
            api_url=api_url,
            headers=headers,
            team_name=team_name,
            channel_name=normalized_channel_name,
        )
        if channel:
            return str(channel["id"])
        raise MattermostAPIError(f"Mattermost channel '{normalized_channel_name}' not found in team '{team_name}'")

    teams_response = await client.get(
        f"{api_url.rstrip('/')}/api/v4/users/me/teams",
        headers=headers,
    )
    teams_response.raise_for_status()

    matches: list[dict[str, Any]] = []
    for team in teams_response.json():
        team_match = await _get_channel_by_team_id(
            client,
            api_url=api_url,
            headers=headers,
            team_id=str(team.get("id", "")),
            channel_name=normalized_channel_name,
        )
        if team_match:
            matches.append(team_match)

    if len(matches) == 1:
        return str(matches[0]["id"])
    if len(matches) > 1:
        raise MattermostAPIError(
            f"Mattermost channel '{normalized_channel_name}' is ambiguous across multiple teams; "
            "set message_bus.team_name or store the channel id on the task"
        )

    raise MattermostAPIError(
        f"Mattermost channel '{normalized_channel_name}' was not found for the configured bot token"
    )


async def create_post(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    bot_token: str,
    text: str,
    channel_id: str = "",
    channel_name: str = "",
    team_id: str = "",
    team_name: str = "",
    root_id: str = "",
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Mattermost post via REST, resolving channel ids when needed."""
    resolved_channel_id = await resolve_channel_id(
        client,
        api_url=api_url,
        bot_token=bot_token,
        channel_id=channel_id,
        channel_name=channel_name,
        team_id=team_id,
        team_name=team_name,
    )

    body: dict[str, Any] = {
        "channel_id": resolved_channel_id,
        "message": text,
    }
    if root_id:
        body["root_id"] = root_id
    if props is not None:
        body["props"] = props

    response = await client.post(
        f"{api_url.rstrip('/')}/api/v4/posts",
        json=body,
        headers=_auth_headers(bot_token),
    )
    response.raise_for_status()
    return response.json()
