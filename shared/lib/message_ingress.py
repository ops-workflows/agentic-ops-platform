"""Provider WebSocket ingress adapters and normalized message events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    provider: str
    message_id: str
    thread_id: str
    channel_id: str
    channel_name: str
    team_id: str
    team_name: str
    user_id: str
    username: str
    text: str
    is_bot: bool = False


@dataclass(frozen=True)
class InteractiveAction:
    provider: str
    action_id: str
    context: dict[str, Any]
    post_id: str
    channel_id: str
    team_id: str
    user_id: str
    username: str


MessageEvent = InboundMessage | InteractiveAction
MessageEventHandler = Callable[[MessageEvent], Awaitable[None]]


def parse_mattermost_websocket_event(payload: dict[str, Any]) -> InboundMessage | None:
    """Normalize a Mattermost ``posted`` WebSocket event."""
    if payload.get("event") != "posted":
        return None

    data = payload.get("data")
    broadcast = payload.get("broadcast")
    if not isinstance(data, dict) or not isinstance(broadcast, dict):
        return None

    raw_post = data.get("post")
    if isinstance(raw_post, str):
        try:
            post = json.loads(raw_post)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_post, dict):
        post = raw_post
    else:
        return None
    if not isinstance(post, dict):
        return None

    message_id = str(post.get("id") or "")
    if not message_id:
        return None
    return InboundMessage(
        provider="mattermost",
        message_id=message_id,
        thread_id=str(post.get("root_id") or message_id),
        channel_id=str(post.get("channel_id") or broadcast.get("channel_id") or ""),
        channel_name=str(data.get("channel_name") or ""),
        team_id=str(data.get("team_id") or broadcast.get("team_id") or ""),
        team_name=str(data.get("team_name") or ""),
        user_id=str(post.get("user_id") or broadcast.get("user_id") or ""),
        username=str(data.get("sender_name") or ""),
        text=str(post.get("message") or "").strip(),
    )


def parse_slack_socket_envelope(payload: dict[str, Any]) -> MessageEvent | None:
    """Normalize Slack Socket Mode event and interactive envelopes."""
    envelope_type = str(payload.get("type") or "")
    body = payload.get("payload")
    if not isinstance(body, dict):
        return None

    if envelope_type == "events_api":
        event = body.get("event")
        if not isinstance(event, dict) or event.get("type") != "message":
            return None
        message_id = str(event.get("ts") or "")
        if not message_id:
            return None
        return InboundMessage(
            provider="slack",
            message_id=message_id,
            thread_id=str(event.get("thread_ts") or message_id),
            channel_id=str(event.get("channel") or ""),
            channel_name=str(event.get("channel") or ""),
            team_id=str(body.get("team_id") or ""),
            team_name="",
            user_id=str(event.get("user") or ""),
            username=str(event.get("user") or ""),
            text=str(event.get("text") or "").strip(),
            is_bot=bool(event.get("bot_id") or event.get("subtype")),
        )

    if envelope_type != "interactive" or body.get("type") != "block_actions":
        return None
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
        return None
    action = actions[0]
    raw_context = action.get("value")
    try:
        context = json.loads(raw_context) if isinstance(raw_context, str) else raw_context
    except json.JSONDecodeError:
        context = {}
    if not isinstance(context, dict):
        context = {}
    container = body.get("container") if isinstance(body.get("container"), dict) else {}
    channel = body.get("channel") if isinstance(body.get("channel"), dict) else {}
    team = body.get("team") if isinstance(body.get("team"), dict) else {}
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    return InteractiveAction(
        provider="slack",
        action_id=str(action.get("action_id") or ""),
        context=context,
        post_id=str(container.get("message_ts") or ""),
        channel_id=str(channel.get("id") or ""),
        team_id=str(team.get("id") or ""),
        user_id=str(user.get("id") or ""),
        username=str(user.get("username") or user.get("name") or user.get("id") or ""),
    )


def mattermost_websocket_url(api_url: str) -> str:
    """Return the WebSocket endpoint for a Mattermost API base URL."""
    parsed = urlparse(api_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/v4/websocket", "", "", ""))


async def _wait_to_reconnect(stop_event: asyncio.Event, delay_sec: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_sec)
    except TimeoutError:
        return


class MattermostWebSocketIngress:
    """Authenticated Mattermost WebSocket consumer with reconnect handling."""

    def __init__(
        self,
        *,
        api_url: str,
        bot_token: str,
        handle_event: MessageEventHandler,
        reconnect_delay_sec: float = 2.0,
    ) -> None:
        self.api_url = api_url
        self.bot_token = bot_token
        self.handle_event = handle_event
        self.reconnect_delay_sec = reconnect_delay_sec

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.api_url or not self.bot_token:
            logger.warning("Mattermost message ingress is disabled because API URL or bot token is missing")
            return

        url = mattermost_websocket_url(self.api_url)
        while not stop_event.is_set():
            try:
                async with connect(url, additional_headers={"Authorization": f"Bearer {self.bot_token}"}) as socket:
                    logger.info("Connected Mattermost message ingress")
                    while not stop_event.is_set():
                        raw = await socket.recv()
                        try:
                            event = parse_mattermost_websocket_event(json.loads(raw))
                        except (TypeError, json.JSONDecodeError):
                            logger.warning("Discarding invalid Mattermost WebSocket payload")
                            continue
                        if event is not None:
                            await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Mattermost message ingress disconnected: %s", exc)
                await _wait_to_reconnect(stop_event, self.reconnect_delay_sec)


class SlackSocketModeIngress:
    """Slack Socket Mode consumer for events and interactive actions."""

    def __init__(
        self,
        *,
        api_url: str,
        app_token: str,
        client: httpx.AsyncClient,
        handle_event: MessageEventHandler,
        reconnect_delay_sec: float = 2.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.app_token = app_token
        self.client = client
        self.handle_event = handle_event
        self.reconnect_delay_sec = reconnect_delay_sec

    async def _open_connection(self) -> str:
        if not self.api_url:
            raise RuntimeError("Slack API URL is required")
        response = await self.client.post(
            f"{self.api_url}/apps.connections.open",
            headers={"Authorization": f"Bearer {self.app_token}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok") or not payload.get("url"):
            raise RuntimeError(f"Slack apps.connections.open failed: {payload.get('error', 'unknown error')}")
        return str(payload["url"])

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.app_token:
            logger.warning(
                "Slack Socket Mode ingress is disabled because message_bus.app_token_secret is not configured"
            )
            return

        while not stop_event.is_set():
            try:
                url = await self._open_connection()
                async with connect(url) as socket:
                    logger.info("Connected Slack Socket Mode ingress")
                    while not stop_event.is_set():
                        raw = await socket.recv()
                        try:
                            envelope = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            logger.warning("Discarding invalid Slack Socket Mode payload")
                            continue
                        envelope_id = str(envelope.get("envelope_id") or "")
                        if envelope_id:
                            await socket.send(json.dumps({"envelope_id": envelope_id}))
                        event = parse_slack_socket_envelope(envelope)
                        if event is not None:
                            await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Slack Socket Mode ingress disconnected: %s", exc)
                await _wait_to_reconnect(stop_event, self.reconnect_delay_sec)


def build_message_ingress(
    *,
    provider: str,
    api_url: str,
    bot_token: str,
    app_token: str,
    client: httpx.AsyncClient,
    handle_event: MessageEventHandler,
) -> MattermostWebSocketIngress | SlackSocketModeIngress:
    """Build the one configured provider listener for the gateway."""
    normalized = provider.strip().lower()
    if normalized == "mattermost":
        return MattermostWebSocketIngress(api_url=api_url, bot_token=bot_token, handle_event=handle_event)
    if normalized == "slack":
        return SlackSocketModeIngress(
            api_url=api_url,
            app_token=app_token,
            client=client,
            handle_event=handle_event,
        )
    raise ValueError(f"Unsupported message bus provider: {provider}")
