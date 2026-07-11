"""Shared helpers for posting to Mattermost through the REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class MattermostAPIError(Exception):
    """Raised when Mattermost REST interaction fails."""


def _auth_headers(bot_token: str) -> dict[str, str]:
    if not bot_token:
        raise MattermostAPIError("Mattermost bot token is required")
    return {"Authorization": f"Bearer {bot_token}"}


def _normalize_channel_name(channel_name: str) -> str:
    return channel_name.strip().lstrip("#")


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
            "set MESSAGE_BUS_TEAM_NAME or store the channel id on the task"
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
