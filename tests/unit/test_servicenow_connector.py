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

    resolved = connector._load_target_workflow_env({"target": {"workflow": "incident-investigator"}})

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
