"""Unit tests for GCP Pub/Sub connector credential setup."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "connectors/gcp-pubsub-connector/main.py"
SPEC = importlib.util.spec_from_file_location("gcp_pubsub_connector_main", MODULE_PATH)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)

pytestmark = pytest.mark.unit


def test_configure_google_credentials_normalizes_literal_newlines(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv(
        "GCP_SERVICE_ACCOUNT_JSON",
        '{"type":"service_account","private_key":"line one\nline two"}',
    )

    main._configure_google_application_credentials()

    credentials_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    with open(credentials_path, encoding="utf-8") as credentials_file:
        assert json.load(credentials_file) == {
            "type": "service_account",
            "private_key": "line one\nline two",
        }


def test_subscriber_callback_uses_connector_event_loop(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create_task(payload, payload_text, attributes, config):
        captured.update(
            payload=payload,
            payload_text=payload_text,
            attributes=attributes,
            config=config,
        )

    class FakeFuture:
        def result(self):
            return None

    class FakeMessage:
        data = b'{"event_id": "event-1"}'
        attributes = {"source": "test"}
        message_id = "message-1"
        acked = False
        nacked = False

        def ack(self):
            self.acked = True

        def nack(self):
            self.nacked = True

    loop = object()
    monkeypatch.setattr(main, "_create_task", fake_create_task)

    def fake_submit(coroutine, submitted_loop):
        captured["loop"] = submitted_loop
        import asyncio

        asyncio.run(coroutine)
        return FakeFuture()

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", fake_submit)
    message = FakeMessage()

    main._subscriber_callback(loop, {"target": {}})(message)

    assert captured["loop"] is loop
    assert captured["payload"] == {"event_id": "event-1"}
    assert message.acked is True
    assert message.nacked is False
