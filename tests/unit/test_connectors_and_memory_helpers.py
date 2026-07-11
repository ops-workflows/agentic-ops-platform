"""Unit tests for connectors catalog + Hindsight response shaping.

These test the pure transformation helpers in ``gateway.api`` that shape
connector YAML and Hindsight API responses into the control-plane's API
models — without any HTTP calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from textwrap import dedent

import pytest

from gateway.api import (
    _connector_source_label,
    _enabled_catalog_ids,
    _extract_hindsight_entries,
    _humanize_identifier,
    _is_text_memory_file,
    _read_connectors_catalog,
    _read_mcp_catalog,
)
from mcps.core import mcp_memory

pytestmark = pytest.mark.unit


# ── Connectors catalog ──────────────────────────────────────────


def _write_connectors_config(tmp_path) -> str:
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        dedent(
            """
            connectors:
              enabled:
                - sf-intake
              instances:
                sf-intake:
                  type: gcp-pubsub
                  display_name: SF Intake
                  source:
                    type: pubsub
                    subscription: my-sub
                  target:
                    workflow: sf-alerts-investigator
                    message_channel: sf-alerts
                  metadata:
                    tags: [GCP, Pub/Sub]
                disabled-one:
                  type: servicenow
                  source:
                    type: polling
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return str(config)


def test_read_connectors_catalog_lists_enabled_instances(monkeypatch, tmp_path):
    from shared.lib.config import settings

    monkeypatch.setattr(settings, "platform_config_file", _write_connectors_config(tmp_path))

    connectors = _read_connectors_catalog()
    ids = {c.id for c in connectors}
    assert ids == {"sf-intake"}


def test_connector_shape_contains_target_and_tags(monkeypatch, tmp_path):
    from shared.lib.config import settings

    monkeypatch.setattr(settings, "platform_config_file", _write_connectors_config(tmp_path))

    connector = next(c for c in _read_connectors_catalog() if c.id == "sf-intake")
    assert connector.name == "SF Intake"
    assert connector.source_type == "pubsub"
    assert connector.type == "gcp-pubsub"
    assert connector.target_workflow == "sf-alerts-investigator"
    assert connector.target_channel == "sf-alerts"
    assert connector.tags == ["GCP", "Pub/Sub"]


def test_catalog_enabled_ids_from_platform_config(monkeypatch, tmp_path):
    from shared.lib.config import settings

    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
mcps:
    enabled:
        - platform
connectors:
    enabled: []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "platform_config_file", str(config))

    assert _enabled_catalog_ids("mcps") == {"platform"}
    assert _enabled_catalog_ids("connectors") == set()
    assert [server.id for server in _read_mcp_catalog()] == ["platform"]
    assert _read_connectors_catalog() == []


def test_connector_source_label_subscription():
    cfg = {"source": {"subscription": "my-sub"}}
    assert _connector_source_label(cfg) == "Pub/Sub subscription my-sub"


def test_connector_source_label_from_metadata_override():
    cfg = {"source": {"subscription": "x"}, "metadata": {"source_label": "Custom label"}}
    assert _connector_source_label(cfg) == "Custom label"


def test_connector_source_label_falls_back_to_name():
    assert _connector_source_label({"name": "my-connector"}) == "My Connector"


# ── Hindsight entry extraction ──────────────────────────────────────────


def test_extract_hindsight_entries_from_items_key():
    payload = {
        "items": [
            {"id": "m1", "content": "first memory", "metadata": {"bank": "x"}, "created_at": "2024-01-01"},
            {"id": "m2", "text": "second memory"},
        ]
    }
    entries = _extract_hindsight_entries(payload, limit=10)
    assert len(entries) == 2
    assert entries[0].id == "m1"
    assert entries[0].content == "first memory"
    assert entries[0].created_at == "2024-01-01"
    assert entries[1].id == "m2"
    assert entries[1].content == "second memory"


def test_extract_hindsight_entries_respects_limit():
    payload = {"items": [{"content": f"m{i}"} for i in range(10)]}
    entries = _extract_hindsight_entries(payload, limit=3)
    assert len(entries) == 3


def test_extract_hindsight_entries_skips_empty_content():
    payload = {"items": [{"content": ""}, {"content": "ok"}, {"content": "   "}]}
    entries = _extract_hindsight_entries(payload, limit=10)
    assert len(entries) == 1
    assert entries[0].content == "ok"


def test_extract_hindsight_entries_from_bare_list():
    payload = [{"content": "bare entry"}]
    entries = _extract_hindsight_entries(payload, limit=10)
    assert len(entries) == 1
    assert entries[0].content == "bare entry"


def test_extract_hindsight_entries_returns_empty_for_unknown_shape():
    assert _extract_hindsight_entries({"no_items": True}, limit=10) == []
    assert _extract_hindsight_entries(None, limit=10) == []


# ── Misc helpers ──────────────────────────────────────────


def test_humanize_identifier_strips_separators():
    assert _humanize_identifier("email-connector") == "Email Connector"
    assert _humanize_identifier("incident_investigator") == "Incident Investigator"


def test_is_text_memory_file_recognizes_common_extensions():
    assert _is_text_memory_file("notes.md") is True
    assert _is_text_memory_file("config.yaml") is True
    assert _is_text_memory_file("output.txt") is True
    # Binary extension
    assert _is_text_memory_file("blob.bin") is False


def test_query_timestamp_for_range_converts_relative_window_to_utc_timestamp():
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    assert mcp_memory._query_timestamp_for_range("24h", now=now) == "2026-04-28T12:00:00Z"
    assert mcp_memory._query_timestamp_for_range("7d", now=now) == "2026-04-22T12:00:00Z"
    assert mcp_memory._query_timestamp_for_range("2w", now=now) == "2026-04-15T12:00:00Z"


def test_query_timestamp_for_range_returns_none_for_unknown_format():
    assert mcp_memory._query_timestamp_for_range("last week") is None
    assert mcp_memory._query_timestamp_for_range("30m") is None


def test_recall_for_digest_includes_query_timestamp_for_relative_window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from shared.lib.config import settings

    recorded: dict[str, object] = {}
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
memory:
  banks:
    business:
            digest-workflow: incident-rca-digest
""".lstrip(),
        encoding="utf-8",
    )

    def fake_request(method: str, endpoint: str, **kwargs):
        recorded["method"] = method
        recorded["endpoint"] = endpoint
        recorded["json"] = kwargs.get("json")
        return {"results": []}

    monkeypatch.setattr(mcp_memory, "_hindsight_request", fake_request)
    monkeypatch.setattr(
        mcp_memory,
        "_query_timestamp_for_range",
        lambda time_range, now=None: "2026-04-28T12:00:00Z",
    )
    monkeypatch.setattr(settings, "platform_config_file", str(config))

    mcp_memory.recall_for_digest(
        query="Summarize recent alerts",
        time_range="24h",
        headers={"x-task-workflow": "digest-workflow"},
    )

    assert recorded["method"] == "POST"
    assert str(recorded["endpoint"]).endswith("/v1/default/banks/incident-rca-digest/memories/recall")
    assert recorded["json"] == {
        "query": (
            "Summarize recent alerts\n"
            "Focus on evidence from the last 24h. Ignore older patterns unless they are still clearly active."
        ),
        "max_tokens": 4096,
        "query_timestamp": "2026-04-28T12:00:00Z",
    }
