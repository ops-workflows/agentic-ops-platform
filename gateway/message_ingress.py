"""Gateway-owned supervision and routing for provider message ingress."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace

import httpx
from sqlalchemy import select

from gateway.message import resolve_approval_action
from gateway.plugin_dir import MessageRoute, discover_all_message_routes
from shared.lib.alert_envelope import parse_alert_envelope
from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.mattermost_api import (
    MattermostAPIError,
)
from shared.lib.mattermost_api import (
    fetch_thread_messages as fetch_mattermost_thread_messages,
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
    fetch_thread_messages as fetch_slack_thread_messages,
)
from shared.lib.slack_api import (
    get_authenticated_user_id as get_slack_user_id,
)
from shared.lib.slack_api import (
    resolve_channel_id as resolve_slack_channel_id,
)
from shared.lib.slack_api import (
    update_post as update_slack_post,
)
from shared.lib.task_queue import create_task

logger = logging.getLogger(__name__)

MAX_ROUTE_INPUT_CHARS = 32_000
THREAD_CONTEXT_MESSAGE_LIMIT = 10
THREAD_CONTEXT_PRIOR_MESSAGE_CHARS = 4_000
_ACCOUNT_PATTERN = re.compile(r"(?<!\d)\d{12}(?!\d)")
_ENVIRONMENT_PATTERN = re.compile(r"(?i)(?<![a-z0-9])(production|prod|uat|development|dev|staging|test)(?![a-z0-9])")
_ENVIRONMENT_ALIASES = {"prod": "production", "dev": "development"}


def _matches_route_constraints(text: str, route: MessageRoute) -> bool:
    if route.allowed_accounts:
        accounts = set(_ACCOUNT_PATTERN.findall(text))
        if not accounts or not accounts.issubset(set(route.allowed_accounts)):
            return False
    if route.allowed_environments:
        environments = {
            _ENVIRONMENT_ALIASES.get(value.lower(), value.lower()) for value in _ENVIRONMENT_PATTERN.findall(text)
        }
        if not environments or not environments.issubset(set(route.allowed_environments)):
            return False
    return True


def _matches_sender_trust(message: InboundMessage, route: MessageRoute) -> bool:
    if route.trusted_user_ids and message.user_id not in route.trusted_user_ids:
        return False
    if not (message.is_bot or message.is_webhook):
        return True
    if not route.allow_bot or not route.trusted_user_ids or message.user_id not in route.trusted_user_ids:
        return False
    if message.is_webhook:
        id_matches = bool(
            route.trusted_webhook_ids and message.webhook_id and message.webhook_id in route.trusted_webhook_ids
        )
        name_matches = bool(
            route.trusted_webhook_names and message.webhook_name and message.webhook_name in route.trusted_webhook_names
        )
        return id_matches or name_matches
    return True


def _match_route_prompt(message: InboundMessage, route: MessageRoute, text: str) -> str | None:
    if route.provider and route.provider != message.provider:
        return None
    if route.root_posts_only and message.thread_id != message.message_id:
        return None
    if not _matches_sender_trust(message, route) or not _matches_route_constraints(text, route):
        return None

    if route.rule_id:
        if route.match_type == "regex":
            match = re.search(route.pattern, text, flags=re.IGNORECASE)
            if match is None:
                return None
            prompt = text if route.preserve_full_body else text[match.end() :].strip()
            return prompt or None
        if not text.lower().startswith(route.pattern.lower()):
            return None
        prompt = text if route.preserve_full_body else text[len(route.pattern) :].strip()
        return prompt or None

    text_lower = text.lower()
    for trigger in route.trigger_words:
        if text_lower.startswith(trigger):
            prompt = text[len(trigger) :].strip()
            if prompt:
                return prompt
    return None


def match_message_route(message: InboundMessage, routes: list[MessageRoute]) -> tuple[MessageRoute, str] | None:
    """Find one deterministic workflow route and its task prompt."""
    channel_values = {message.channel_id.strip().lower(), message.channel_name.strip().lower()}
    channel_values.discard("")
    text = message.text.strip()[:MAX_ROUTE_INPUT_CHARS]
    matches: dict[tuple[object, ...], tuple[MessageRoute, str]] = {}
    for route in routes:
        if route.channel not in channel_values:
            continue
        prompt = _match_route_prompt(message, route, text)
        if prompt is None:
            continue
        identity = (route.workflow, route.rule_id, route.match_type, route.pattern, route.trigger_words)
        matches.setdefault(identity, (route, prompt))
    if not matches:
        return None
    highest_priority = max(route.priority for route, _prompt in matches.values())
    winners = [match for match in matches.values() if match[0].priority == highest_priority]
    if len(winners) != 1:
        logger.error(
            "Rejecting ambiguous message route at priority %d: %s",
            highest_priority,
            [f"{route.workflow}:{route.rule_id or 'trigger'}" for route, _prompt in winners],
        )
        return None
    return winners[0]


class GatewayMessageIngress:
    """Owns one configured provider listener within the gateway service."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._listener_task: asyncio.Task[None] | None = None
        self._bot_user_id = ""
        self._routes: list[MessageRoute] = []

    @property
    def running(self) -> bool:
        return self._listener_task is not None and not self._listener_task.done()

    async def start(self) -> None:
        if self.running:
            return
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
        self._stop_event = asyncio.Event()
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
                resolved_routes.append(replace(route, channel=channel_id.lower()))
        return resolved_routes

    async def pause(self) -> None:
        await self._stop_listener()

    async def resume(self) -> None:
        await self.start()

    async def _stop_listener(self) -> None:
        self._stop_event.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

    async def stop(self) -> None:
        await self._stop_listener()
        await self._client.aclose()

    async def handle_event(self, event: MessageEvent) -> None:
        if isinstance(event, InboundMessage):
            await self._handle_message(event)
        else:
            await self._handle_interactive_action(event)

    async def _handle_message(self, message: InboundMessage) -> None:
        if self._bot_user_id and message.user_id == self._bot_user_id:
            return
        if not message.text:
            return

        if not (message.is_bot or message.is_webhook) and await self._record_question_reply(message):
            return

        matched = match_message_route(message, self._routes)
        if matched is None:
            return
        route, prompt = matched
        envelope = parse_alert_envelope(prompt) if route.rule_id else None
        hydrated_prompt = await self._hydrate_thread_prompt(message, prompt)
        if hydrated_prompt is None:
            return
        prompt, thread_context_count = hydrated_prompt
        metadata = {
            "source": message.provider,
            "user": message.username,
            "user_id": message.user_id,
            "channel_id": message.channel_id,
            "channel": message.channel_name,
            "team_id": message.team_id,
            "team_domain": message.team_name,
            "is_bot": message.is_bot,
            "is_webhook": message.is_webhook,
            "webhook_id": message.webhook_id,
            "webhook_name": message.webhook_name,
            "text_truncated": message.text_truncated,
            "message_id": message.message_id,
            "route_rule_id": route.rule_id,
            "thread_context_count": thread_context_count,
        }
        if envelope is not None:
            metadata["alert_envelope"] = envelope.as_metadata()
            metadata["alert_occurrence_count"] = 1
            metadata["alert_state_transitions"] = [{"state": envelope.state, "message_id": message.message_id}]
        async with async_session_factory() as session:
            await create_task(
                session,
                workflow=route.workflow,
                prompt=prompt,
                channel=message.provider,
                metadata=metadata,
                message_channel=message.channel_name or message.channel_id,
                message_thread=message.thread_id,
                coalesce_key=envelope.coalesce_key(route.workflow) if envelope is not None else None,
                coalesce_window_sec=route.coalesce_window_sec,
            )
        await self._post_acknowledgement(message)

    async def _hydrate_thread_prompt(self, message: InboundMessage, prompt: str) -> tuple[str, int] | None:
        if message.message_id == message.thread_id:
            return prompt, 0

        try:
            if message.provider == "mattermost":
                messages = await fetch_mattermost_thread_messages(
                    self._client,
                    api_url=settings.message_bus.api_url,
                    bot_token=settings.message_bus.bot_token,
                    root_id=message.thread_id,
                    current_message_id=message.message_id,
                    current_message_created_at_ms=message.created_at_ms,
                    limit=THREAD_CONTEXT_MESSAGE_LIMIT,
                )
            elif message.provider == "slack":
                messages = await fetch_slack_thread_messages(
                    self._client,
                    api_url=settings.message_bus.api_url,
                    bot_token=settings.message_bus.bot_token,
                    channel_id=message.channel_id,
                    thread_id=message.thread_id,
                    current_message_id=message.message_id,
                    limit=THREAD_CONTEXT_MESSAGE_LIMIT,
                )
            else:
                return prompt, 0
        except (MattermostAPIError, SlackAPIError, httpx.HTTPError, ValueError) as exc:
            logger.warning("Unable to hydrate %s thread %s: %s", message.provider, message.thread_id, exc)
            return None

        normalized_messages = [dict(item) for item in messages if item.get("text")]
        current = next(
            (item for item in normalized_messages if item.get("message_id") == message.message_id),
            None,
        )
        if current is None:
            normalized_messages.append(
                {
                    "message_id": message.message_id,
                    "author": message.username or message.user_id or "unknown",
                    "text": prompt,
                }
            )
        else:
            current["author"] = message.username or message.user_id or current.get("author") or "unknown"
            current["text"] = prompt

        normalized_messages = normalized_messages[-THREAD_CONTEXT_MESSAGE_LIMIT:]
        rendered = [
            "Thread conversation (oldest to newest; the final message is the task request):",
        ]
        for index, item in enumerate(normalized_messages, start=1):
            text = str(item["text"])
            max_chars = (
                MAX_ROUTE_INPUT_CHARS if index == len(normalized_messages) else THREAD_CONTEXT_PRIOR_MESSAGE_CHARS
            )
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "\n[message truncated]"
            rendered.extend(
                [
                    "",
                    f"Message {index} from {item.get('author') or 'unknown'}:",
                    text,
                ]
            )
        return "\n".join(rendered), len(normalized_messages)

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
