"""Unit coverage for workflow-owned ServiceNow connection settings."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _load_connector_module():
    path = Path(__file__).parents[2] / "connectors" / "servicenow-connector" / "main.py"
    spec = importlib.util.spec_from_file_location("servicenow_connector_main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_target_workflow_env_uses_target_agent_config(monkeypatch) -> None:
    connector = _load_connector_module()
    package = SimpleNamespace(
        config={
            "env": {"SERVICENOW_INSTANCE_URL": "https://workflow.service-now.com"},
            "secrets": {
                "SERVICENOW_USERNAME": {"encrypted": "ENC[age,username]"},
                "SERVICENOW_PASSWORD": {"encrypted": "ENC[age,password]"},
            },
        }
    )
    monkeypatch.setattr(connector, "find_workflow_package", lambda workflow: package)
    monkeypatch.setattr(
        connector,
        "decrypt_agent_secrets",
        lambda config, *, identity: {
            "SERVICENOW_USERNAME": "workflow-user",
            "SERVICENOW_PASSWORD": "workflow-password",
        },
    )
    monkeypatch.setenv("AGE_IDENTITY", "test-identity")
    monkeypatch.setenv("SERVICENOW_INSTANCE_URL", "https://platform.example.com")

    resolved = connector._load_target_workflow_env({"target": {"workflow": "example-workflow"}})

    assert resolved == {
        "SERVICENOW_INSTANCE_URL": "https://workflow.service-now.com",
        "SERVICENOW_USERNAME": "workflow-user",
        "SERVICENOW_PASSWORD": "workflow-password",
    }


def test_load_target_workflow_env_requires_existing_target(monkeypatch) -> None:
    connector = _load_connector_module()
    monkeypatch.setattr(connector, "find_workflow_package", lambda workflow: None)

    with pytest.raises(RuntimeError, match="was not found"):
        connector._load_target_workflow_env({"target": {"workflow": "missing-workflow"}})


def test_parse_record_and_prompt_are_driven_by_instance_config() -> None:
    connector = _load_connector_module()
    config = {
        "parsing": {"extract": {"record_id": "number", "owner": "assigned_to.display_value"}},
        "target": {"prompt_template": "Handle {record_id} for {owner}; missing={missing}"},
    }

    parsed = connector._parse_record(
        {"number": "INC001", "assigned_to": {"display_value": "Ada"}, "custom": "private"},
        config,
    )

    assert parsed["record_id"] == "INC001"
    assert parsed["owner"] == "Ada"
    assert "private" in parsed["raw_record"]
    assert connector._build_prompt(parsed, config) == "Handle INC001 for Ada; missing="


def test_load_instance_config_requires_explicit_coalescing_key(monkeypatch) -> None:
    connector = _load_connector_module()
    monkeypatch.setenv("CONNECTOR_INSTANCE_ID", "records")
    monkeypatch.setattr(
        connector,
        "load_connector_instance",
        lambda path, instance_id: {
            "source": {"table": "change_request"},
            "target": {"workflow": "example-workflow", "prompt_template": "Handle {record_id}"},
            "coalescing": {"enabled": True},
        },
    )

    with pytest.raises(RuntimeError, match="coalescing.key_field"):
        connector._load_instance_config()


def test_load_instance_config_uses_single_enabled_servicenow_instance(monkeypatch) -> None:
    connector = _load_connector_module()
    monkeypatch.delenv("CONNECTOR_INSTANCE_ID", raising=False)
    monkeypatch.setattr(
        connector,
        "load_enabled_connector_instance",
        lambda path, connector_type: (
            "records",
            {
                "source": {"table": "change_request"},
                "target": {"workflow": "example-workflow", "prompt_template": "Handle {record_id}"},
            },
        ),
    )

    config = connector._load_instance_config()

    assert config["_instance_id"] == "records"
