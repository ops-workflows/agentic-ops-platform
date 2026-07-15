"""Unit tests for structured platform-config loaders (MCP config + connector instances)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from shared.lib.platform_secrets import (
    expand_env_placeholders,
    load_connector_instance,
    load_connector_instances,
    load_enabled_connector_instance,
    load_mcp_server_config,
)

pytestmark = pytest.mark.unit


def _write(path: Path, body: str) -> str:
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return str(path)


def test_expand_env_placeholders_recurses_and_defaults_missing():
    env = {"X": "1", "Y": "svc"}
    value = {"a": "${X}", "b": ["${Y}", "static"], "c": {"d": "${MISSING}"}}
    assert expand_env_placeholders(value, env) == {"a": "1", "b": ["svc", "static"], "c": {"d": ""}}


def test_load_connector_instances_expands_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "proj-123")
    monkeypatch.setenv("GCP_PUBSUB_SUBSCRIPTION", "subs")
    config = _write(
        tmp_path / "platform-config.yaml",
        """
        connectors:
          enabled:
            - sf-intake
          instances:
            sf-intake:
              type: gcp-pubsub
              source:
                type: pubsub
                project: ${GCP_PROJECT}
                subscription: ${GCP_PUBSUB_SUBSCRIPTION}
              target:
                workflow: sf-alerts-investigator
        """,
    )

    instances = load_connector_instances(config)
    assert set(instances) == {"sf-intake"}
    assert instances["sf-intake"]["source"]["project"] == "proj-123"
    assert instances["sf-intake"]["source"]["subscription"] == "subs"

    one = load_connector_instance(config, "sf-intake")
    assert one["type"] == "gcp-pubsub"
    assert one["target"]["workflow"] == "sf-alerts-investigator"

    assert load_connector_instance(config, "missing") == {}


def test_load_connector_instances_missing_section_returns_empty(tmp_path):
    config = _write(tmp_path / "platform-config.yaml", "config: {}\n")
    assert load_connector_instances(config) == {}


def test_load_enabled_connector_instance_selects_one_matching_enabled_instance(tmp_path):
    config = _write(
        tmp_path / "platform-config.yaml",
        """
        connectors:
          enabled:
            - sf-intake
          instances:
            sf-intake:
              type: gcp-pubsub
            disabled-servicenow:
              type: servicenow
        """,
    )

    instance_id, instance = load_enabled_connector_instance(config, "gcp-pubsub")
    assert instance_id == "sf-intake"
    assert instance == {"type": "gcp-pubsub"}

    assert load_enabled_connector_instance(config, "servicenow") == ("", {})


def test_load_mcp_server_config_returns_expanded_block(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_API_VERSION", "v61.0")
    config = _write(
        tmp_path / "platform-config.yaml",
        """
        mcps:
          enabled:
            - salesforce
          config:
            salesforce:
              api_version: ${SF_API_VERSION}
              allowed_objects:
                - Case
                - Account
              object_fields:
                Case: "Id, Subject"
        """,
    )

    block = load_mcp_server_config(config, "salesforce")
    assert block["api_version"] == "v61.0"
    assert block["allowed_objects"] == ["Case", "Account"]
    assert block["object_fields"] == {"Case": "Id, Subject"}

    assert load_mcp_server_config(config, "missing") == {}
