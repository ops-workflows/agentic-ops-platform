"""Layer 0 - provider event normalization for message ingress."""

from __future__ import annotations

from gateway.message_ingress import GatewayMessageIngress, match_message_route
from gateway.plugin_dir import MessageRoute
from shared.lib.message_ingress import (
    InboundMessage,
    InteractiveAction,
    SlackSocketModeIngress,
    mattermost_websocket_url,
    parse_mattermost_websocket_event,
    parse_slack_socket_envelope,
)


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
                    '"user_id":"user-1","message":"@ops investigate"}'
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
    assert match_message_route(
        InboundMessage(**{**message.__dict__, "text": "investigate this"}), routes
    ) is None


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
