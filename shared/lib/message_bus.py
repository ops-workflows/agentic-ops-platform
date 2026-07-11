"""Provider-neutral message bus adapters for human communication."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from shared.lib.mattermost_api import MattermostAPIError, create_post

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageRef:
    id: str
    thread_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class MessageReply:
    message: str
    user_id: str = ""
    username: str = ""
    raw: dict[str, Any] | None = None


class MessageBus(Protocol):
    async def post_to_thread(self, text: str) -> MessageRef | None: ...

    async def wait_for_reply(
        self,
        *,
        started_after_ms: int,
        ignore_message_ids: set[str] | None = None,
        timeout_sec: int = 3600,
    ) -> MessageReply | None: ...


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

    async def post_to_thread(self, text: str) -> MessageRef | None:
        if not self.channel_name and not self.channel_id:
            return None
        if not self.bot_token:
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
            logger.warning("Failed to post message-bus thread message: %s", exc)
            return None

        message_id = str(post.get("id") or "")
        thread_id = self.get_thread_id() or message_id
        if thread_id and not self.get_thread_id():
            self.set_thread_id(thread_id)
        return MessageRef(id=message_id, thread_id=thread_id, raw=post)

    async def wait_for_reply(
        self,
        *,
        started_after_ms: int,
        ignore_message_ids: set[str] | None = None,
        timeout_sec: int = 3600,
    ) -> MessageReply | None:
        if not self.bot_token or not self.get_thread_id():
            return None

        client = await self.client_factory()
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            resp = await client.get(
                f"{self.api_url}/api/v4/posts/{self.get_thread_id()}/thread",
                headers={"Authorization": f"Bearer {self.bot_token}"},
                timeout=30,
            )
            resp.raise_for_status()
            reply = _first_human_reply(resp.json(), started_after_ms, ignore_message_ids or set())
            if reply:
                return reply
            await asyncio.sleep(5)

        return None


def _first_human_reply(
    payload: dict[str, Any], started_after_ms: int, ignore_message_ids: set[str]
) -> MessageReply | None:
    posts = payload.get("posts", {})
    users = payload.get("users", {}) if isinstance(payload.get("users"), dict) else {}
    ordered_posts = sorted(posts.values(), key=lambda post: post.get("create_at", 0))
    for post in ordered_posts:
        if post.get("create_at", 0) <= started_after_ms:
            continue
        post_id = str(post.get("id") or "")
        if post_id and post_id in ignore_message_ids:
            continue
        message = (post.get("message") or "").strip()
        if not message:
            continue
        user_id = str(post.get("user_id") or "").strip()
        return MessageReply(
            message=message,
            user_id=user_id,
            username=_username_for_user(users.get(user_id) if user_id else None),
            raw=post,
        )
    return None


def _username_for_user(profile: Any) -> str:
    if not isinstance(profile, dict):
        return ""
    username = str(profile.get("username") or "").strip()
    if username:
        return username
    email = str(profile.get("email") or "").strip()
    if email:
        return email
    first_name = str(profile.get("first_name") or "").strip()
    last_name = str(profile.get("last_name") or "").strip()
    return f"{first_name} {last_name}".strip()


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
        self.api_url = (api_url or "https://slack.com/api").rstrip("/")
        self.bot_token = bot_token
        self.channel = channel
        self.get_thread_id = get_thread_id
        self.set_thread_id = set_thread_id

    async def post_to_thread(self, text: str) -> MessageRef | None:
        if not self.channel or not self.bot_token:
            return None

        client = await self.client_factory()
        thread_ts = self.get_thread_id()
        body: dict[str, Any] = {"channel": self.channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts

        try:
            response = await client.post(
                f"{self.api_url}/chat.postMessage",
                json=body,
                headers={"Authorization": f"Bearer {self.bot_token}"},
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to post Slack message: %s", exc)
            return None

        payload = response.json()
        if not payload.get("ok"):
            logger.warning("Slack chat.postMessage rejected: %s", payload.get("error"))
            return None

        message_ts = str(payload.get("ts") or "")
        resolved_thread = thread_ts or message_ts
        if resolved_thread and not thread_ts:
            self.set_thread_id(resolved_thread)
        return MessageRef(id=message_ts, thread_id=resolved_thread, raw=payload)

    async def wait_for_reply(
        self,
        *,
        started_after_ms: int,
        ignore_message_ids: set[str] | None = None,
        timeout_sec: int = 3600,
    ) -> MessageReply | None:
        thread_ts = self.get_thread_id()
        if not self.bot_token or not self.channel or not thread_ts:
            return None

        client = await self.client_factory()
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            response = await client.get(
                f"{self.api_url}/conversations.replies",
                params={"channel": self.channel, "ts": thread_ts},
                headers={"Authorization": f"Bearer {self.bot_token}"},
                timeout=30,
            )
            response.raise_for_status()
            reply = _first_slack_human_reply(response.json(), thread_ts, started_after_ms, ignore_message_ids or set())
            if reply:
                return reply
            await asyncio.sleep(5)

        return None


def _first_slack_human_reply(
    payload: dict[str, Any], thread_ts: str, started_after_ms: int, ignore_message_ids: set[str]
) -> MessageReply | None:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        ts = str(message.get("ts") or "")
        if not ts or ts == thread_ts:
            continue
        if message.get("bot_id") or message.get("subtype"):
            continue
        if ts in ignore_message_ids:
            continue
        try:
            created_ms = int(float(ts) * 1000)
        except ValueError:
            continue
        if created_ms <= started_after_ms:
            continue
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        user_id = str(message.get("user") or "").strip()
        return MessageReply(message=text, user_id=user_id, username=user_id, raw=message)
    return None


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
    normalized = (provider or "mattermost").strip().lower()
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
