from __future__ import annotations

import pytest

from shared.lib.mattermost_api import MattermostAPIError
from shared.lib.message_bus import (
    MattermostMessageBus,
    SlackMessageBus,
    build_message_bus,
)
from shared.lib.platform_secrets import load_message_bus_config, load_platform_env
from shared.lib.slack_api import resolve_channel_id

pytestmark = pytest.mark.unit


def test_platform_config_loads_message_bus_without_environment_aliases(tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
message_bus:
    provider: mattermost
    api_url: http://mattermost:8065
    team_name: platform
""".lstrip(),
        encoding="utf-8",
    )

    message_bus = load_message_bus_config(str(config))

    assert message_bus.provider == "mattermost"
    assert message_bus.api_url == "http://mattermost:8065"
    assert message_bus.team_name == "platform"


def test_platform_config_decrypts_message_bus_secret_references(tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
message_bus:
    provider: mattermost
    bot_token_secret: message_bus_bot_token
    action_callback_secret: message_action_callback_secret
secrets:
    message_bus_bot_token:
        encrypted: ENC[plain,bot-token]
    message_action_callback_secret:
        encrypted: ENC[plain,callback-token]
""".lstrip(),
        encoding="utf-8",
    )

    message_bus = load_message_bus_config(str(config), identity="unused")

    assert message_bus.bot_token == "bot-token"
    assert message_bus.action_callback_secret == "callback-token"


def test_platform_environment_does_not_include_message_bus_secrets(tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
message_bus:
    provider: mattermost
    bot_token_secret: message_bus_bot_token
secrets:
    message_bus_bot_token:
        encrypted: ENC[plain,bot-token]
    OTHER_TOKEN:
        encrypted: ENC[plain,other-token]
""".lstrip(),
        encoding="utf-8",
    )

    env = load_platform_env(str(config), identity="unused")

    assert "message_bus_bot_token" not in env
    assert env["OTHER_TOKEN"] == "other-token"


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(self._payload)

    async def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(self._payload)


def _thread_state() -> tuple[dict, callable, callable]:
    state = {"thread": ""}
    return state, (lambda: state["thread"]), (lambda value: state.__setitem__("thread", value))


def test_build_message_bus_selects_provider():
    state, get_thread, set_thread = _thread_state()
    common = {
        "client_factory": lambda: None,
        "api_url": "http://host",
        "bot_token": "t",
        "get_thread_id": get_thread,
        "set_thread_id": set_thread,
    }
    assert isinstance(build_message_bus(provider="mattermost", **common), MattermostMessageBus)
    assert isinstance(build_message_bus(provider="slack", channel_id="C1", **common), SlackMessageBus)
    for provider in ("", "teams"):
        with pytest.raises(ValueError):
            build_message_bus(provider=provider, **common)


async def test_slack_channel_name_resolution_uses_visible_channel_id():
    client = _FakeClient(
        {
            "ok": True,
            "channels": [
                {"id": "C111", "name": "other"},
                {"id": "C123", "name": "operations"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    )

    channel_id = await resolve_channel_id(
        client,
        api_url="https://slack.example.test/api",
        bot_token="xoxb-test",
        channel_name="#operations",
    )

    assert channel_id == "C123"
    url, kwargs = client.calls[0]
    assert url.endswith("/conversations.list")
    assert kwargs["params"]["types"] == "public_channel,private_channel"


async def test_mattermost_post_preserves_provider_failure(monkeypatch):
    state, get_thread, set_thread = _thread_state()

    async def factory():
        return _FakeClient({})

    async def fail_post(*args, **kwargs):
        raise MattermostAPIError("Mattermost channel 'vishal-test' was not found")

    monkeypatch.setattr("shared.lib.message_bus.create_post", fail_post)
    bus = MattermostMessageBus(
        client_factory=factory,
        api_url="https://mattermost.example.test",
        bot_token="token",
        channel_name="vishal-test",
        get_thread_id=get_thread,
        set_thread_id=set_thread,
    )

    assert await bus.post_to_thread("hello") is None
    assert bus.last_error == "Mattermost channel 'vishal-test' was not found"
    assert state["thread"] == ""


async def test_slack_post_to_thread_sets_thread_id():
    client = _FakeClient({"ok": True, "ts": "1700000000.000100"})
    state, get_thread, set_thread = _thread_state()

    async def factory():
        return client

    bus = SlackMessageBus(
        client_factory=factory,
        api_url="https://slack.com/api",
        bot_token="xoxb-1",
        channel="C123",
        get_thread_id=get_thread,
        set_thread_id=set_thread,
    )

    ref = await bus.post_to_thread("hello")

    assert ref is not None
    assert ref.id == "1700000000.000100"
    assert state["thread"] == "1700000000.000100"
    url, kwargs = client.calls[0]
    assert url.endswith("/chat.postMessage")
    assert kwargs["json"]["channel"] == "C123"


async def test_slack_post_to_thread_returns_none_on_error_payload():
    client = _FakeClient({"ok": False, "error": "channel_not_found"})
    _, get_thread, set_thread = _thread_state()

    async def factory():
        return client

    bus = SlackMessageBus(
        client_factory=factory,
        api_url="https://slack.com/api",
        bot_token="xoxb-1",
        channel="C123",
        get_thread_id=get_thread,
        set_thread_id=set_thread,
    )

    assert await bus.post_to_thread("hello") is None


async def test_slack_post_requires_explicit_api_url():
    client = _FakeClient({"ok": True, "ts": "1700000000.000100"})
    _, get_thread, set_thread = _thread_state()

    async def factory():
        return client

    bus = SlackMessageBus(
        client_factory=factory,
        api_url="",
        bot_token="xoxb-1",
        channel="C123",
        get_thread_id=get_thread,
        set_thread_id=set_thread,
    )

    assert await bus.post_to_thread("hello") is None
    assert client.calls == []
