"""Provider-neutral message bus adapters for human communication."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from shared.lib.mattermost_api import MattermostAPIError, create_post
from shared.lib.slack_api import SlackAPIError
from shared.lib.slack_api import create_post as create_slack_post

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageRef:
    id: str
    thread_id: str
    raw: dict[str, Any]


class MessageBus(Protocol):
    async def post_to_thread(self, text: str) -> MessageRef | None: ...


class MattermostMessageBus:
    """Mattermost-backed MessageBus implementation."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[httpx.AsyncClient]],
        api_url: str,
        bot_token: str,
        channel_id: str = "",
        channel_name: str = "",
        team_id: str = "",
        team_name: str = "",
        get_thread_id: Callable[[], str],
        set_thread_id: Callable[[str], None],
    ) -> None:
        self.client_factory = client_factory
        self.api_url = api_url
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.team_id = team_id
        self.team_name = team_name
        self.get_thread_id = get_thread_id
        self.set_thread_id = set_thread_id
        self.last_error = ""

    async def post_to_thread(self, text: str) -> MessageRef | None:
        self.last_error = ""
        if not self.channel_name and not self.channel_id:
            self.last_error = "No Mattermost channel name or channel ID is available for this task"
            return None
        if not self.bot_token:
            self.last_error = "No Mattermost bot token is available for this task"
            return None

        client = await self.client_factory()
        try:
            post = await create_post(
                client,
                api_url=self.api_url,
                bot_token=self.bot_token,
                text=text,
                channel_id=self.channel_id,
                channel_name=self.channel_name,
                team_id=self.team_id,
                team_name=self.team_name,
                root_id=self.get_thread_id(),
            )
        except (MattermostAPIError, httpx.HTTPError) as exc:
            self.last_error = str(exc)
            logger.warning("Failed to post message-bus thread message: %s", self.last_error)
            return None

        message_id = str(post.get("id") or "")
        thread_id = self.get_thread_id() or message_id
        if thread_id and not self.get_thread_id():
            self.set_thread_id(thread_id)
        return MessageRef(id=message_id, thread_id=thread_id, raw=post)

class SlackMessageBus:
    """Slack-backed MessageBus implementation using the Web API."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[httpx.AsyncClient]],
        api_url: str,
        bot_token: str,
        channel: str,
        get_thread_id: Callable[[], str],
        set_thread_id: Callable[[str], None],
    ) -> None:
        self.client_factory = client_factory
        self.api_url = api_url.strip().rstrip("/")
        self.bot_token = bot_token
        self.channel = channel
        self.get_thread_id = get_thread_id
        self.set_thread_id = set_thread_id

    async def post_to_thread(self, text: str) -> MessageRef | None:
        if not self.channel or not self.bot_token:
            return None

        client = await self.client_factory()
        thread_ts = self.get_thread_id()
        try:
            payload = await create_slack_post(
                client,
                api_url=self.api_url,
                bot_token=self.bot_token,
                channel_id=self.channel,
                text=text,
                thread_id=thread_ts,
            )
        except (SlackAPIError, httpx.HTTPError) as exc:
            logger.warning("Failed to post Slack message: %s", exc)
            return None

        message_ts = str(payload.get("ts") or "")
        resolved_thread = thread_ts or message_ts
        if resolved_thread and not thread_ts:
            self.set_thread_id(resolved_thread)
        return MessageRef(id=message_ts, thread_id=resolved_thread, raw=payload)

def build_message_bus(
    *,
    provider: str,
    client_factory: Callable[[], Awaitable[httpx.AsyncClient]],
    api_url: str,
    bot_token: str,
    get_thread_id: Callable[[], str],
    set_thread_id: Callable[[str], None],
    channel_id: str = "",
    channel_name: str = "",
    team_id: str = "",
    team_name: str = "",
) -> MessageBus:
    """Build the MessageBus implementation for the configured provider."""
    normalized = provider.strip().lower()
    if normalized == "mattermost":
        return MattermostMessageBus(
            client_factory=client_factory,
            api_url=api_url,
            bot_token=bot_token,
            channel_id=channel_id,
            channel_name=channel_name,
            team_id=team_id,
            team_name=team_name,
            get_thread_id=get_thread_id,
            set_thread_id=set_thread_id,
        )
    if normalized == "slack":
        return SlackMessageBus(
            client_factory=client_factory,
            api_url=api_url,
            bot_token=bot_token,
            channel=channel_id or channel_name,
            get_thread_id=get_thread_id,
            set_thread_id=set_thread_id,
        )
    raise ValueError(f"Unsupported message bus provider: {provider!r}")


async def post_channel_message(
    provider: str,
    *,
    api_url: str,
    bot_token: str,
    text: str,
    channel_id: str = "",
    channel_name: str = "",
    team_id: str = "",
    team_name: str = "",
    thread_root: str = "",
) -> MessageRef | None:
    """Post a one-shot platform notification through the configured provider.

    Used by platform-side posts (task completion, lost/timed-out notices) so they
    stay provider-neutral instead of calling a provider REST client directly.
    """
    thread_id = thread_root

    async with httpx.AsyncClient(timeout=10.0) as client:

        async def client_factory() -> httpx.AsyncClient:
            return client

        bus = build_message_bus(
            provider=provider,
            client_factory=client_factory,
            api_url=api_url,
            bot_token=bot_token,
            channel_id=channel_id,
            channel_name=channel_name,
            team_id=team_id,
            team_name=team_name,
            get_thread_id=lambda: thread_id,
            set_thread_id=lambda _value: None,
        )
        return await bus.post_to_thread(text)
