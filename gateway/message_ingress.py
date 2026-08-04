"""Gateway-owned supervision and routing for provider message ingress."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
from sqlalchemy import select

from gateway.message import resolve_approval_action
from gateway.plugin_dir import MessageRoute, discover_all_message_routes
from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.mattermost_api import (
    MattermostAPIError,
)
from shared.lib.mattermost_api import (
    get_authenticated_user_id as get_mattermost_user_id,
)
from shared.lib.mattermost_api import (
    resolve_channel_id as resolve_mattermost_channel_id,
)
from shared.lib.message_bus import build_message_bus
from shared.lib.message_ingress import (
    InboundMessage,
    InteractiveAction,
    MessageEvent,
    build_message_ingress,
)
from shared.lib.models import SessionEvent, Task
from shared.lib.slack_api import (
    SlackAPIError,
)
from shared.lib.slack_api import (
    get_authenticated_user_id as get_slack_user_id,
)
from shared.lib.slack_api import (
    resolve_channel_id as resolve_slack_channel_id,
)
from shared.lib.slack_api import update_post as update_slack_post
from shared.lib.task_queue import create_task

logger = logging.getLogger(__name__)


def match_message_route(message: InboundMessage, routes: list[MessageRoute]) -> tuple[MessageRoute, str] | None:
    """Find the workflow route and prompt after a matching trigger prefix."""
    channel_values = {message.channel_id.strip().lower(), message.channel_name.strip().lower()}
    channel_values.discard("")
    text = message.text.strip()
    text_lower = text.lower()
    for route in routes:
        if route.channel not in channel_values:
            continue
        for trigger in route.trigger_words:
            if not text_lower.startswith(trigger):
                continue
            prompt = text[len(trigger) :].strip()
            if prompt:
                return route, prompt
    return None


class GatewayMessageIngress:
    """Owns one configured provider listener within the gateway service."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._listener_task: asyncio.Task[None] | None = None
        self._bot_user_id = ""
        self._routes: list[MessageRoute] = []

    async def start(self) -> None:
        provider = settings.message_bus.provider
        if provider not in {"mattermost", "slack"}:
            logger.warning("Message ingress disabled for unsupported provider %s", provider)
            return
        if not settings.message_bus.bot_token:
            logger.warning("Message ingress disabled because message_bus.bot_token_secret is not configured")
            return
        if not settings.message_bus.api_url:
            logger.warning("Message ingress disabled because message_bus.api_url is missing")
            return
        if provider == "slack" and not settings.message_bus.app_token:
            logger.warning(
                "Slack Socket Mode ingress is disabled because message_bus.app_token_secret is not configured"
            )
            return

        try:
            if provider == "mattermost":
                self._bot_user_id = await get_mattermost_user_id(
                    self._client,
                    api_url=settings.message_bus.api_url,
                    bot_token=settings.message_bus.bot_token,
                )
            else:
                self._bot_user_id = await get_slack_user_id(
                    self._client,
                    api_url=settings.message_bus.api_url,
                    bot_token=settings.message_bus.bot_token,
                )
        except (MattermostAPIError, SlackAPIError, httpx.HTTPError) as exc:
            logger.warning("Message ingress could not identify the configured bot: %s", exc)

        self._routes = await self._resolve_route_channels(provider)
        listener = build_message_ingress(
            provider=provider,
            api_url=settings.message_bus.api_url,
            bot_token=settings.message_bus.bot_token,
            app_token=settings.message_bus.app_token,
            client=self._client,
            handle_event=self.handle_event,
        )
        self._listener_task = asyncio.create_task(listener.run(self._stop_event), name=f"{provider}-message-ingress")

    async def _resolve_route_channels(self, provider: str) -> list[MessageRoute]:
        """Add provider channel-ID aliases to workflow-owned channel-name routes."""
        routes = discover_all_message_routes()
        resolved_routes = list(routes)
        for route in routes:
            try:
                if provider == "mattermost":
                    channel_id = await resolve_mattermost_channel_id(
                        self._client,
                        api_url=settings.message_bus.api_url,
                        bot_token=settings.message_bus.bot_token,
                        channel_name=route.channel,
                        team_name=settings.message_bus.team_name,
                    )
                else:
                    channel_id = await resolve_slack_channel_id(
                        self._client,
                        api_url=settings.message_bus.api_url,
                        bot_token=settings.message_bus.bot_token,
                        channel_name=route.channel,
                    )
            except (MattermostAPIError, SlackAPIError, httpx.HTTPError) as exc:
                logger.warning(
                    "Could not resolve configured %s message channel %r for workflow %s: %s",
                    provider,
                    route.channel,
                    route.workflow,
                    exc,
                )
                continue
            if channel_id and channel_id.lower() != route.channel:
                resolved_routes.append(
                    MessageRoute(
                        workflow=route.workflow,
                        channel=channel_id.lower(),
                        trigger_words=route.trigger_words,
                    )
                )
        return resolved_routes

    async def stop(self) -> None:
        self._stop_event.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener_task
        await self._client.aclose()

    async def handle_event(self, event: MessageEvent) -> None:
        if isinstance(event, InboundMessage):
            await self._handle_message(event)
        else:
            await self._handle_interactive_action(event)

    async def _handle_message(self, message: InboundMessage) -> None:
        if message.is_bot or (self._bot_user_id and message.user_id == self._bot_user_id):
            return
        if not message.text:
            return

        if await self._record_question_reply(message):
            return

        matched = match_message_route(message, self._routes or discover_all_message_routes())
        if matched is None:
            return
        route, prompt = matched
        async with async_session_factory() as session:
            await create_task(
                session,
                workflow=route.workflow,
                prompt=prompt,
                channel=message.provider,
                metadata={
                    "source": message.provider,
                    "user": message.username,
                    "user_id": message.user_id,
                    "channel_id": message.channel_id,
                    "channel": message.channel_name,
                    "team_id": message.team_id,
                    "team_domain": message.team_name,
                },
                message_channel=message.channel_name or message.channel_id,
                message_thread=message.thread_id,
            )
        await self._post_acknowledgement(message)

    async def _record_question_reply(self, message: InboundMessage) -> bool:
        async with async_session_factory() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.status == "waiting_user_input",
                    Task.channel == message.provider,
                    Task.message_thread == message.thread_id,
                )
                .with_for_update()
            )
            if task is None:
                return False
            session.add(
                SessionEvent(
                    task_id=task.id,
                    event_type="user_question_reply",
                    data={
                        "message": message.text,
                        "message_id": message.message_id,
                        "provider": message.provider,
                        "user_id": message.user_id,
                        "username": message.username,
                        "thread_id": message.thread_id,
                    },
                )
            )
            await session.commit()
            logger.info("Recorded message reply for task %s", task.id)
            return True

    async def _post_acknowledgement(self, message: InboundMessage) -> None:
        async def client_factory() -> httpx.AsyncClient:
            return self._client

        thread_id = message.thread_id
        bus = build_message_bus(
            provider=message.provider,
            client_factory=client_factory,
            api_url=settings.message_bus.api_url,
            bot_token=settings.message_bus.bot_token,
            channel_id=message.channel_id,
            channel_name=message.channel_name,
            team_id=message.team_id,
            team_name=message.team_name,
            get_thread_id=lambda: thread_id,
            set_thread_id=lambda _thread_id: None,
        )
        posted = await bus.post_to_thread(":brain: Working on it...")
        if posted is None:
            logger.warning("Unable to post message acknowledgement for %s", message.message_id)

    async def _handle_interactive_action(self, action: InteractiveAction) -> None:
        if action.provider != "slack" or action.action_id != "agentic_ops_approval":
            return
        async with async_session_factory() as session:
            try:
                resolution = await resolve_approval_action(
                    session,
                    context=action.context,
                    user_id=action.user_id,
                    post_id=action.post_id,
                    channel_id=action.channel_id,
                    source="slack_interactive",
                )
            except ValueError as exc:
                logger.warning("Rejected Slack approval action: %s", exc)
                return

        try:
            await update_slack_post(
                self._client,
                api_url=settings.message_bus.api_url,
                bot_token=settings.message_bus.bot_token,
                channel_id=action.channel_id,
                post_id=action.post_id,
                text=resolution.status_message,
            )
        except (SlackAPIError, httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Approval state resolved but Slack prompt update failed: %s", exc)


async def with_message_ingress(run: Callable[[GatewayMessageIngress], Awaitable[None]]) -> None:
    """Test helper that guarantees the gateway ingress client is shut down."""
    ingress = GatewayMessageIngress()
    try:
        await run(ingress)
    finally:
        await ingress.stop()
