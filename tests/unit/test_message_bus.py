from __future__ import annotations

import pytest

from shared.lib.mattermost_api import MattermostAPIError
from shared.lib.message_bus import (
    MattermostMessageBus,
    SlackMessageBus,
    _first_human_reply,
    _first_slack_human_reply,
    build_message_bus,
)
from shared.lib.platform_secrets import load_platform_env

pytestmark = pytest.mark.unit


def test_first_human_reply_ignores_old_and_prompt_posts():
    payload = {
        "posts": {
            "old": {"id": "old", "create_at": 100, "message": "too old", "user_id": "u1"},
            "prompt": {"id": "prompt", "create_at": 300, "message": "question", "user_id": "bot"},
            "reply": {"id": "reply", "create_at": 400, "message": "2", "user_id": "u1"},
        },
        "users": {"u1": {"username": "operator"}},
    }

    reply = _first_human_reply(payload, started_after_ms=200, ignore_message_ids={"prompt"})

    assert reply is not None
    assert reply.message == "2"
    assert reply.user_id == "u1"
    assert reply.username == "operator"


def test_platform_config_loads_message_bus_provider(tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
config:
    MESSAGE_BUS_API_URL: http://mattermost:8065
message_bus:
    provider: mattermost
""".lstrip(),
        encoding="utf-8",
    )

    env = load_platform_env(str(config))

    assert env["MESSAGE_BUS_API_URL"] == "http://mattermost:8065"
    assert env["MESSAGE_BUS_PROVIDER"] == "mattermost"


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
    with pytest.raises(ValueError):
        build_message_bus(provider="teams", **common)


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


def test_first_slack_human_reply_skips_parent_bots_and_old():
    payload = {
        "messages": [
            {"ts": "1700000100.000000", "text": "parent prompt"},
            {"ts": "1700000150.000000", "text": "bot echo", "bot_id": "B1", "user": "U0"},
            {"ts": "1700000090.000000", "text": "too old", "user": "U1"},
            {"ts": "1700000200.000000", "text": "the answer", "user": "U2"},
        ]
    }
    reply = _first_slack_human_reply(
        payload,
        thread_ts="1700000100.000000",
        started_after_ms=1700000100_000,
        ignore_message_ids=set(),
    )
    assert reply is not None
    assert reply.message == "the answer"
    assert reply.user_id == "U2"
