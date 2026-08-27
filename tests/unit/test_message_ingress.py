"""Layer 0 - provider event normalization for message ingress."""

from __future__ import annotations

import httpx

from gateway.message_ingress import GatewayMessageIngress, match_message_route
from gateway.plugin_dir import MessageRoute
from shared.lib.mattermost_api import fetch_thread_messages as fetch_mattermost_thread_messages
from shared.lib.message_ingress import (
    InboundMessage,
    InteractiveAction,
    SlackSocketModeIngress,
    mattermost_websocket_url,
    parse_mattermost_websocket_event,
    parse_slack_socket_envelope,
)
from shared.lib.slack_api import fetch_thread_messages as fetch_slack_thread_messages


class _ThreadResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _ThreadClient:
    def __init__(self, *, mattermost_payload: dict | None = None, slack_pages: list[dict] | None = None) -> None:
        self.mattermost_payload = mattermost_payload or {}
        self.slack_pages = list(slack_pages or [])
        self.calls: list[tuple[str, str, dict]] = []

    async def get(self, url: str, **kwargs) -> _ThreadResponse:
        self.calls.append(("GET", url, kwargs))
        return _ThreadResponse(self.mattermost_payload)

    async def post(self, url: str, **kwargs) -> _ThreadResponse:
        self.calls.append(("POST", url, kwargs))
        return _ThreadResponse(self.slack_pages.pop(0))


def test_parse_mattermost_posted_event() -> None:
    event = parse_mattermost_websocket_event(
        {
            "event": "posted",
            "data": {
                "channel_name": "operations",
                "team_id": "team-1",
                "sender_name": "alice",
                "post": (
                    '{"id":"post-1","root_id":"root-1","channel_id":"channel-1",'
                    '"user_id":"user-1","message":"@ops investigate","create_at":1234}'
                ),
            },
            "broadcast": {"channel_id": "channel-1", "team_id": "team-1"},
        }
    )

    assert event == InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="root-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="",
        user_id="user-1",
        username="alice",
        text="@ops investigate",
        created_at_ms=1234,
    )


def test_parse_mattermost_rich_webhook_alert() -> None:
    event = parse_mattermost_websocket_event(
        {
            "event": "posted",
            "data": {
                "channel_name": "online-monitoring",
                "team_id": "team-1",
                "sender_name": "company-webhooks",
                "post": {
                    "id": "post-1",
                    "channel_id": "channel-1",
                    "user_id": "webhook-user-1",
                    "message": "",
                    "props": {
                        "from_bot": "true",
                        "from_webhook": "true",
                        "webhook_id": "webhook-1",
                        "webhook_display_name": "AWS CloudWatch",
                        "attachments": [
                            {
                                "fallback": "production-api-gatewayAlarm is ALARM",
                                "title": "production-api-gatewayAlarm",
                                "title_link": "https://console.aws.example/alarm",
                                "fields": [
                                    {"title": "State", "value": "ALARM"},
                                    {"title": "Account", "value": "production"},
                                ],
                            }
                        ],
                    },
                },
            },
            "broadcast": {"channel_id": "channel-1", "team_id": "team-1"},
        }
    )

    assert event == InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="post-1",
        channel_id="channel-1",
        channel_name="online-monitoring",
        team_id="team-1",
        team_name="",
        user_id="webhook-user-1",
        username="company-webhooks",
        text=(
            "production-api-gatewayAlarm is ALARM\n"
            "production-api-gatewayAlarm\n"
            "https://console.aws.example/alarm\n"
            "State: ALARM\n"
            "Account: production"
        ),
        is_bot=True,
        is_webhook=True,
        webhook_id="webhook-1",
        webhook_name="AWS CloudWatch",
    )


def test_parse_slack_message_envelope() -> None:
    event = parse_slack_socket_envelope(
        {
            "type": "events_api",
            "payload": {
                "team_id": "team-1",
                "event": {
                    "type": "message",
                    "channel": "C123",
                    "user": "U123",
                    "ts": "123.456",
                    "text": "@ops investigate",
                },
            },
        }
    )

    assert event == InboundMessage(
        provider="slack",
        message_id="123.456",
        thread_id="123.456",
        channel_id="C123",
        channel_name="C123",
        team_id="team-1",
        team_name="",
        user_id="U123",
        username="U123",
        text="@ops investigate",
    )


def test_parse_slack_interactive_action() -> None:
    event = parse_slack_socket_envelope(
        {
            "type": "interactive",
            "payload": {
                "type": "block_actions",
                "team": {"id": "team-1"},
                "user": {"id": "U123", "username": "alice"},
                "channel": {"id": "C123"},
                "container": {"message_ts": "123.456"},
                "actions": [
                    {
                        "action_id": "agentic_ops_approval",
                        "value": '{"approval_id":"approval-1","decision":"approve"}',
                    }
                ],
            },
        }
    )

    assert event == InteractiveAction(
        provider="slack",
        action_id="agentic_ops_approval",
        context={"approval_id": "approval-1", "decision": "approve"},
        post_id="123.456",
        channel_id="C123",
        team_id="team-1",
        user_id="U123",
        username="alice",
    )


def test_mattermost_websocket_url_uses_secure_scheme() -> None:
    assert mattermost_websocket_url("https://mattermost.example/") == "wss://mattermost.example/api/v4/websocket"
    assert mattermost_websocket_url("http://mattermost:8065") == "ws://mattermost:8065/api/v4/websocket"


async def test_mattermost_thread_fetch_returns_latest_ten_in_order() -> None:
    posts = {
        f"post-{index}": {
            "id": f"post-{index}",
            "create_at": index,
            "user_id": f"user-{index}",
            "message": f"message {index}",
        }
        for index in range(1, 13)
    }
    client = _ThreadClient(mattermost_payload={"posts": posts})

    messages = await fetch_mattermost_thread_messages(
        client,
        api_url="https://mattermost.example",
        bot_token="token",
        root_id="post-1",
        current_message_id="post-12",
        current_message_created_at_ms=12,
        limit=10,
    )

    assert [message["message_id"] for message in messages] == [f"post-{index}" for index in range(3, 13)]
    method, url, kwargs = client.calls[0]
    assert method == "GET"
    assert url.endswith("/api/v4/posts/post-1/thread")
    assert kwargs["params"] == {
        "perPage": 10,
        "fromPost": "post-12",
        "fromCreateAt": 12,
        "direction": "up",
    }


async def test_slack_thread_fetch_paginates_before_selecting_latest_ten() -> None:
    client = _ThreadClient(
        slack_pages=[
            {
                "ok": True,
                "messages": [
                    {"ts": f"1000.{index:06d}", "user": f"U{index}", "text": f"message {index}"}
                    for index in range(1, 9)
                ],
                "response_metadata": {"next_cursor": "page-2"},
            },
            {
                "ok": True,
                "messages": [
                    {"ts": f"1000.{index:06d}", "user": f"U{index}", "text": f"message {index}"}
                    for index in range(9, 13)
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )

    messages = await fetch_slack_thread_messages(
        client,
        api_url="https://slack.example/api",
        bot_token="token",
        channel_id="C123",
        thread_id="1000.000001",
        current_message_id="1000.000012",
        limit=10,
    )

    assert [message["message_id"] for message in messages] == [f"1000.{index:06d}" for index in range(3, 13)]
    assert [call[2]["json"].get("cursor", "") for call in client.calls] == ["", "page-2"]
    assert all(call[2]["json"]["latest"] == "1000.000012" for call in client.calls)


async def test_gateway_hydrates_reply_and_strips_trigger_from_final_request(monkeypatch) -> None:
    async def fetch_messages(*_args, **_kwargs) -> list[dict[str, str]]:
        return [
            {"message_id": "root-1", "author": "alice", "text": "API errors started after deploy"},
            {"message_id": "post-2", "author": "bob", "text": "I see elevated latency too"},
            {"message_id": "post-3", "author": "user-3", "text": "@agent investigate this"},
        ]

    monkeypatch.setattr("gateway.message_ingress.fetch_mattermost_thread_messages", fetch_messages)
    ingress = GatewayMessageIngress()
    message = InboundMessage(
        provider="mattermost",
        message_id="post-3",
        thread_id="root-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="company",
        user_id="user-3",
        username="carol",
        text="@agent investigate this",
    )
    try:
        prompt, count = await ingress._hydrate_thread_prompt(message, "investigate this")
    finally:
        await ingress.stop()

    assert count == 3
    assert "Message 1 from alice:\nAPI errors started after deploy" in prompt
    assert "Message 3 from carol:\ninvestigate this" in prompt
    assert "@agent" not in prompt


async def test_gateway_does_not_fetch_thread_for_root_post(monkeypatch) -> None:
    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("root posts must not fetch thread history")

    monkeypatch.setattr("gateway.message_ingress.fetch_slack_thread_messages", unexpected_fetch)
    ingress = GatewayMessageIngress()
    message = InboundMessage(
        provider="slack",
        message_id="1000.000001",
        thread_id="1000.000001",
        channel_id="C123",
        channel_name="C123",
        team_id="T123",
        team_name="",
        user_id="U123",
        username="U123",
        text="@agent investigate this",
    )
    try:
        prompt, count = await ingress._hydrate_thread_prompt(message, "investigate this")
    finally:
        await ingress.stop()

    assert prompt == "investigate this"
    assert count == 0


async def test_gateway_stops_when_required_thread_history_is_unavailable(monkeypatch) -> None:
    async def unavailable_history(*_args, **_kwargs):
        raise httpx.ConnectError("history unavailable")

    monkeypatch.setattr("gateway.message_ingress.fetch_mattermost_thread_messages", unavailable_history)
    ingress = GatewayMessageIngress()
    message = InboundMessage(
        provider="mattermost",
        message_id="post-2",
        thread_id="root-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="example",
        user_id="user-2",
        username="bob",
        text="@agent investigate this",
    )
    try:
        result = await ingress._hydrate_thread_prompt(message, "investigate this")
    finally:
        await ingress.stop()

    assert result is None


async def test_gateway_filters_reply_before_fetching_thread(monkeypatch) -> None:
    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("rejected messages must not fetch thread history")

    async def not_a_question_reply(_message) -> bool:
        return False

    monkeypatch.setattr("gateway.message_ingress.fetch_mattermost_thread_messages", unexpected_fetch)
    ingress = GatewayMessageIngress()
    monkeypatch.setattr(ingress, "_record_question_reply", not_a_question_reply)
    ingress._routes = [
        MessageRoute(
            workflow="incident-investigator",
            channel="operations",
            trigger_words=("@agent",),
        )
    ]
    message = InboundMessage(
        provider="mattermost",
        message_id="post-2",
        thread_id="root-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="company",
        user_id="user-2",
        username="bob",
        text="ordinary reply without a trigger",
    )
    try:
        await ingress._handle_message(message)
    finally:
        await ingress.stop()


async def test_slack_socket_mode_uses_configured_api_url() -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"ok": True, "url": "wss://socket.example"}

    class Client:
        def __init__(self) -> None:
            self.url = ""

        async def post(self, url: str, **_kwargs) -> Response:
            self.url = url
            return Response()

    async def handle_event(_event) -> None:
        return None

    client = Client()
    ingress = SlackSocketModeIngress(
        api_url="https://slack-proxy.example/api/",
        app_token="xapp-test",
        client=client,
        handle_event=handle_event,
    )

    assert await ingress._open_connection() == "wss://socket.example"
    assert client.url == "https://slack-proxy.example/api/apps.connections.open"


def test_match_message_route_requires_configured_channel_and_trigger() -> None:
    message = InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="post-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="",
        user_id="user-1",
        username="alice",
        text="@ops investigate this",
    )
    routes = [
        MessageRoute(workflow="incident-investigator", channel="operations", trigger_words=("@ops",)),
        MessageRoute(workflow="other", channel="operations", trigger_words=("@other",)),
    ]

    assert match_message_route(message, routes) == (routes[0], "investigate this")
    assert match_message_route(InboundMessage(**{**message.__dict__, "text": "investigate this"}), routes) is None


def test_match_message_route_accepts_trusted_production_webhook() -> None:
    message = InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="post-1",
        channel_id="channel-1",
        channel_name="online-monitoring",
        team_id="team-1",
        team_name="company",
        user_id="user-1",
        username="company-webhooks",
        text="production-api-gatewayAlarm\nAccount: 123456789012\nState: ALARM",
        is_bot=True,
        is_webhook=True,
        webhook_name="AWS CloudWatch",
    )
    route = MessageRoute(
        workflow="online-alerts-investigator",
        channel="online-monitoring",
        rule_id="production-alarm",
        provider="mattermost",
        priority=100,
        match_type="regex",
        pattern=r"production-.*?Alarm",
        trusted_user_ids=("user-1",),
        trusted_webhook_names=("AWS CloudWatch",),
        allow_bot=True,
        root_posts_only=True,
        preserve_full_body=True,
        allowed_accounts=("123456789012",),
        allowed_environments=("production",),
    )

    assert match_message_route(message, [route]) == (route, message.text)


def test_match_message_route_rejects_untrusted_or_nonproduction_webhook() -> None:
    trusted_route = MessageRoute(
        workflow="online-alerts-investigator",
        channel="online-monitoring",
        rule_id="production-alarm",
        provider="mattermost",
        priority=100,
        match_type="regex",
        pattern=r"production-.*?Alarm",
        trusted_user_ids=("user-1",),
        trusted_webhook_names=("AWS CloudWatch",),
        allow_bot=True,
        root_posts_only=True,
        preserve_full_body=True,
        allowed_accounts=("123456789012",),
        allowed_environments=("production",),
    )
    base = InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="post-1",
        channel_id="channel-1",
        channel_name="online-monitoring",
        team_id="team-1",
        team_name="company",
        user_id="user-1",
        username="company-webhooks",
        text="production-api-gatewayAlarm\nAccount: 123456789012",
        is_bot=True,
        is_webhook=True,
        webhook_name="Wrong webhook",
    )

    assert match_message_route(base, [trusted_route]) is None
    assert (
        match_message_route(
            InboundMessage(
                **{
                    **base.__dict__,
                    "webhook_name": "AWS CloudWatch",
                    "text": "uat-api-gatewayAlarm\nAccount: 184285746512",
                }
            ),
            [trusted_route],
        )
        is None
    )


def test_match_message_route_rejects_reply_and_equal_priority_ambiguity() -> None:
    message = InboundMessage(
        provider="mattermost",
        message_id="post-2",
        thread_id="root-1",
        channel_id="channel-1",
        channel_name="operations",
        team_id="team-1",
        team_name="",
        user_id="user-1",
        username="alice",
        text="production-api-gatewayAlarm",
    )
    root_only = MessageRoute(
        workflow="one",
        channel="operations",
        rule_id="one",
        priority=100,
        match_type="regex",
        pattern="production-.*?Alarm",
        root_posts_only=True,
        preserve_full_body=True,
    )
    assert match_message_route(message, [root_only]) is None

    root_message = InboundMessage(**{**message.__dict__, "thread_id": "post-2"})
    competing = MessageRoute(
        workflow="two",
        channel="operations",
        rule_id="two",
        priority=100,
        match_type="regex",
        pattern="production-.*?Alarm",
        preserve_full_body=True,
    )
    assert match_message_route(root_message, [root_only, competing]) is None


async def test_gateway_resolves_workflow_channel_names_to_slack_id_aliases(monkeypatch) -> None:
    configured_route = MessageRoute(
        workflow="incident-investigator",
        channel="operations",
        trigger_words=("@ops",),
    )

    async def resolve_channel(*_args, **kwargs) -> str:
        assert kwargs["channel_name"] == "operations"
        return "C123"

    monkeypatch.setattr("gateway.message_ingress.discover_all_message_routes", lambda: [configured_route])
    monkeypatch.setattr("gateway.message_ingress.resolve_slack_channel_id", resolve_channel)
    ingress = GatewayMessageIngress()
    try:
        routes = await ingress._resolve_route_channels("slack")
    finally:
        await ingress.stop()

    assert routes == [
        configured_route,
        MessageRoute(workflow="incident-investigator", channel="c123", trigger_words=("@ops",)),
    ]


async def test_gateway_creates_structured_alert_task_with_envelope_and_coalesce_key(monkeypatch) -> None:
    route = MessageRoute(
        workflow="online-alerts-investigator",
        channel="online-monitoring",
        rule_id="production-alarm",
        provider="mattermost",
        priority=100,
        match_type="regex",
        pattern=r"production-.*?Alarm",
        trusted_user_ids=("user-1",),
        trusted_webhook_names=("AWS CloudWatch",),
        allow_bot=True,
        root_posts_only=True,
        preserve_full_body=True,
        allowed_accounts=("123456789012",),
        allowed_environments=("production",),
        coalesce_window_sec=90,
    )
    message = InboundMessage(
        provider="mattermost",
        message_id="post-1",
        thread_id="post-1",
        channel_id="channel-1",
        channel_name="online-monitoring",
        team_id="team-1",
        team_name="company",
        user_id="user-1",
        username="company-webhooks",
        text=(
            "Status: firing\n"
            "Alertname: production-api-gatewayAlarm\n"
            "Labels: alertarn=arn:aws:cloudwatch:eu-west-1:123456789012:"
            "alarm:production-api-gatewayAlarm, environment=production"
        ),
        is_bot=True,
        is_webhook=True,
        webhook_name="AWS CloudWatch",
    )
    captured: dict[str, object] = {}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args) -> None:
            return None

    async def create_task(_session, **kwargs):
        captured.update(kwargs)
        return object()

    async def acknowledge(_message) -> None:
        return None

    monkeypatch.setattr("gateway.message_ingress.async_session_factory", SessionContext)
    monkeypatch.setattr("gateway.message_ingress.create_task", create_task)
    ingress = GatewayMessageIngress()
    ingress._routes = [route]
    monkeypatch.setattr(ingress, "_post_acknowledgement", acknowledge)
    try:
        await ingress._handle_message(message)
    finally:
        await ingress.stop()

    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["message_id"] == "post-1"
    assert metadata["route_rule_id"] == "production-alarm"
    assert metadata["alert_envelope"] == {
        "version": 1,
        "source": "aws",
        "identity": "production-api-gatewayAlarm",
        "state": "firing",
        "account": "123456789012",
        "region": "eu-west-1",
        "environment": "production",
    }
    assert str(captured["coalesce_key"]).startswith("alert:v1:")
    assert captured["coalesce_window_sec"] == 90
