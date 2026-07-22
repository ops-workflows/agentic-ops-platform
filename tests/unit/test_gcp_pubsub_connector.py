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


def test_parse_gcs_email_prefers_plain_text_and_extracts_headers():
    raw = b"""From: Salesforce <info@salesforce.com>
To: alerts@example.com
Subject: Flow failure
Date: Fri, 17 Jul 2026 04:14:10 +0000
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary=part

--part
Content-Type: text/plain; charset=utf-8

Plain failure details.
--part
Content-Type: text/html; charset=utf-8

<p>HTML failure details.</p>
--part--
"""

    parsed = main._parse_gcs_email(raw, "alert_email_text", 4_000)

    assert parsed["email_subject"] == "Flow failure"
    assert parsed["email_sender"] == "Salesforce <info@salesforce.com>"
    assert parsed["email_recipient"] == "alerts@example.com"
    assert parsed["alert_email_text"] == "Plain failure details."
    assert parsed["email_body_text"] == "Plain failure details."


def test_parse_gcs_email_converts_html_when_plain_text_is_absent():
    raw = b"""Subject: HTML-only alert
Content-Type: text/html; charset=utf-8

<h1>Flow error</h1><p>Record <strong>001</strong> failed.</p>
"""

    parsed = main._parse_gcs_email(raw, "email_text", 4_000)

    assert parsed["email_text"] == "Flow error Record 001 failed."


def test_extract_email_metadata_uses_deployment_regexes():
    metadata = {
        "email_subject": "Developer script exception from SubscriptionTrigger",
        "email_sender": "ApexApplication <info@salesforce.com>",
        "email_body_text": "Failure for record a2IVc000000lHS1MAM: FIELD_CUSTOM_VALIDATION_EXCEPTION",
    }
    config = {
        "parsing": {
            "email_extract": {
                "apex_class": "Developer script exception from ([^:\\n]+)",
                "error_type": "(FIELD_CUSTOM_VALIDATION_EXCEPTION)",
                "record_ids": "\\b([a-zA-Z0-9]{18})\\b",
                "description": "body",
            }
        }
    }

    extracted = main._extract_email_metadata(metadata, config)

    assert extracted == {
        "apex_class": "SubscriptionTrigger",
        "error_type": "FIELD_CUSTOM_VALIDATION_EXCEPTION",
        "record_ids": "a2IVc000000lHS1MAM",
        "description": metadata["email_body_text"],
    }


def test_render_prompt_omits_missing_parsed_values():
    rendered = main._render_prompt("Flow: {flow_name}\nApex: {apex_class}", {"flow_name": None})

    assert rendered == "Flow: \nApex: "
