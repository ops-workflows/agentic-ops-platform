"""Unit tests for the config-driven Salesforce MCP policy loading."""

from __future__ import annotations

import importlib
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = pytest.mark.unit


def _write_policy(path: Path, body: str) -> str:
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return str(path)


def _reload_with_config(monkeypatch, config_path: str):
    monkeypatch.setenv("PLATFORM_CONFIG_FILE", config_path)
    import mcps.integrations.mcp_salesforce as sf

    return importlib.reload(sf)


def test_policy_loads_from_platform_config(monkeypatch, tmp_path):
    config = _write_policy(
        tmp_path / "platform-config.yaml",
        """
        mcps:
          config:
            salesforce:
              api_version: v55.0
              max_query_limit: 50
              max_query_fields: 5
              allowed_objects:
                - Case
                - Account
              allowed_tooling_objects:
                - ValidationRule
              filter_required_objects:
                - Account
              object_fields:
                Case: "Id, Subject, Status"
              tooling_object_fields:
                ValidationRule: "Id, ValidationName"
        """,
    )

    sf = _reload_with_config(monkeypatch, config)

    assert sf.API_VERSION == "v55.0"
    assert sf.MAX_QUERY_LIMIT == 50
    assert sf.MAX_QUERY_FIELDS == 5
    assert sorted(sf.ALLOWED_OBJECTS) == ["Account", "Case"]
    assert sorted(sf.ALLOWED_TOOLING_OBJECTS) == ["ValidationRule"]
    assert sorted(sf.FILTER_REQUIRED_OBJECTS) == ["Account"]
    assert sf.OBJECT_FIELDS["Case"] == "Id, Subject, Status"
    assert sf.TOOLING_OBJECT_FIELDS["ValidationRule"] == "Id, ValidationName"


def test_no_hardcoded_policy_without_config(monkeypatch, tmp_path):
    config = _write_policy(tmp_path / "platform-config.yaml", "config: {}\n")

    sf = _reload_with_config(monkeypatch, config)

    # The public server ships no built-in org policy: absent config means no
    # allowed objects and safe scalar defaults.
    assert not sf.ALLOWED_OBJECTS
    assert not sf.ALLOWED_TOOLING_OBJECTS
    assert not sf.OBJECT_FIELDS
    assert sf.API_VERSION == sf.DEFAULT_API_VERSION
    assert sf.MAX_QUERY_LIMIT == sf.DEFAULT_MAX_QUERY_LIMIT
    assert sf.MAX_QUERY_FIELDS == sf.DEFAULT_MAX_QUERY_FIELDS


def test_default_fields_fall_back_to_generic_set(monkeypatch, tmp_path):
    config = _write_policy(
        tmp_path / "platform-config.yaml",
        """
        mcps:
          config:
            salesforce:
              allowed_objects:
                - Case
        """,
    )

    sf = _reload_with_config(monkeypatch, config)

    assert sf._default_fields_for_object("Case") == "Id, Name, CreatedDate, LastModifiedDate"
    assert sf._default_fields_for_tooling_object("ValidationRule") == "Id"
