"""Layer 0 — fake services standalone.

Validate that the fake services behave as their contracts advertise,
before any platform code talks to them.
"""

from __future__ import annotations

import httpx
import pytest

from tests.fakes.hindsight import build_fake_hindsight
from tests.fakes.message import build_fake_mattermost
from tests.fakes.mock_llm import Turn, build_mock_llm

pytestmark = pytest.mark.unit


def test_fake_mattermost_records_posts(background_app) -> None:
    fake = build_fake_mattermost()
    server = background_app(fake.app)

    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        resp = client.post(
            "/api/v4/posts",
            json={"channel_id": "chan-1", "message": "hello"},
        )
        assert resp.status_code == 200
        post = resp.json()
        assert post["channel_id"] == "chan-1"

        assert fake.posts_in_thread(post["id"])[0].message == "hello"


def test_fake_mattermost_rejects_missing_channel(background_app) -> None:
    fake = build_fake_mattermost()
    server = background_app(fake.app)
    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        resp = client.post("/api/v4/posts", json={"message": "x"})
        assert resp.status_code == 400


def test_fake_message_provider_supports_slack_post_and_update(background_app) -> None:
    fake = build_fake_mattermost()
    server = background_app(fake.app)
    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        created = client.post("/chat.postMessage", json={"channel": "C123", "text": "hello"}).json()
        assert created["ok"] is True
        updated = client.post("/chat.update", json={"channel": "C123", "ts": created["ts"], "text": "resolved"}).json()
        assert updated["ok"] is True
    assert fake.all_posts()[0].message == "resolved"


def test_fake_message_provider_returns_mattermost_and_slack_threads(background_app) -> None:
    fake = build_fake_mattermost()
    fake.seed_post(post_id="root", channel_id="C123", message="root message", user_id="user-1")
    fake.seed_post(
        post_id="reply",
        channel_id="C123",
        root_id="root",
        message="reply message",
        user_id="user-2",
    )
    server = background_app(fake.app)

    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        mattermost = client.get("/api/v4/posts/root/thread").json()
        slack = client.post("/conversations.replies", json={"channel": "C123", "ts": "root"}).json()

    assert mattermost["order"] == ["root", "reply"]
    assert mattermost["posts"]["reply"]["message"] == "reply message"
    assert [message["text"] for message in slack["messages"]] == ["root message", "reply message"]


def test_fake_hindsight_records_retain_and_scripted_recall(background_app) -> None:
    fake = build_fake_hindsight()
    server = background_app(fake.app)

    fake.script_recall([{"case_id": "C-1", "summary": "similar incident"}])

    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/banks").json()["banks"]

        client.post("/v1/banks/platform-test-bank/retain", json={"case_id": "X"})
        recall = client.post("/v1/banks/platform-test-bank/recall", json={"query": "x"}).json()
        assert recall == {"items": [{"case_id": "C-1", "summary": "similar incident"}]}

    ops = [r["op"] for r in fake.recorded_requests()]
    assert "list_banks" in ops
    assert "retain" in ops
    assert "recall" in ops


def test_mock_llm_non_streaming_returns_scripted_response(background_app) -> None:
    llm = build_mock_llm(
        turns=[
            Turn(
                expect={"markers_present": ["PLATFORM_TEST_MARK"]},
                respond=[{"type": "text", "text": "hi"}],
                stop_reason="end_turn",
                usage={"input_tokens": 2, "output_tokens": 3},
            )
        ]
    )
    server = background_app(llm.app)

    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-test",
                "messages": [{"role": "user", "content": "hello PLATFORM_TEST_MARK"}],
                "stream": False,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_reason"] == "end_turn"
    assert body["content"][0]["text"] == "hi"
    assert llm.expectation_failures() == []


def test_mock_llm_streaming_emits_sse_events(background_app) -> None:
    llm = build_mock_llm(
        turns=[
            Turn(
                respond=[{"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}],
                stop_reason="tool_use",
            )
        ]
    )
    server = background_app(llm.app)

    with (
        httpx.Client(base_url=server.base_url, timeout=5) as client,
        client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-test",
                "messages": [{"role": "user", "content": "run bash"}],
                "stream": True,
            },
        ) as resp,
    ):
        chunks = b"".join(list(resp.iter_bytes())).decode()

    assert "event: message_start" in chunks
    assert "event: content_block_start" in chunks
    assert "event: content_block_delta" in chunks
    assert "event: message_stop" in chunks


def test_mock_llm_records_expectation_failures(background_app) -> None:
    llm = build_mock_llm(
        turns=[Turn(expect={"markers_present": ["NEVER_THERE"]}, respond=[{"type": "text", "text": "x"}])]
    )
    server = background_app(llm.app)
    with httpx.Client(base_url=server.base_url, timeout=5) as client:
        client.post(
            "/v1/messages",
            json={"model": "claude-test", "messages": [{"role": "user", "content": "hi"}]},
        )
    failures = llm.expectation_failures()
    assert any("NEVER_THERE" in f for f in failures)
