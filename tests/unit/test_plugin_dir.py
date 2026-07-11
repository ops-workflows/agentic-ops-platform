"""Layer 0 — plugin directory helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from gateway.plugin_dir import (  # noqa: E402
    discover_message_routes,
    discover_plugin_configs,
    read_platform_config,
    read_plugin_files,
    validate_plugin_dir,
)
from shared.lib.workflow_paths import discover_workflow_packages  # noqa: E402


def test_read_platform_config_returns_parsed_agent_yaml(test_plugin_dir: Path) -> None:
    cfg = read_platform_config(test_plugin_dir)
    assert cfg["name"] == "platform-test"
    assert cfg["session"]["max_turns"] == 8
    assert cfg["runtime"]["container_image"] == "ai-ops-agent-runtime:latest"


def test_read_platform_config_missing_returns_empty(tmp_path: Path) -> None:
    assert read_platform_config(tmp_path) == {}


def test_validate_plugin_dir_is_clean(test_plugin_dir: Path) -> None:
    errors = validate_plugin_dir(test_plugin_dir)
    assert errors == [], errors


def test_validate_plugin_dir_missing_files(tmp_path: Path) -> None:
    errors = validate_plugin_dir(tmp_path)
    assert any("agent.yaml" in e for e in errors)
    assert any(".mcp.json" in e for e in errors)
    assert any("settings.json" in e for e in errors)
    assert any("agents" in e for e in errors)


def test_validate_plugin_dir_bad_mcp_json(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text("name: x\n")
    (tmp_path / "settings.json").write_text("{}\n")
    (tmp_path / ".mcp.json").write_text("not json")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.md").write_text("---\nname: x\n---\n")
    errors = validate_plugin_dir(tmp_path)
    assert any(".mcp.json is not valid JSON" in e for e in errors)


def test_validate_plugin_dir_missing_mcp_servers_key(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text("name: x\n")
    (tmp_path / "settings.json").write_text("{}\n")
    (tmp_path / ".mcp.json").write_text(json.dumps({"wrong": {}}))
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.md").write_text("---\nname: x\n---\n")
    errors = validate_plugin_dir(tmp_path)
    assert any("'mcpServers'" in e for e in errors)


def test_validate_plugin_dir_agent_yaml_schema_violation(tmp_path: Path) -> None:
    # Missing required "description" and an invalid (non-kebab-case) "name".
    (tmp_path / "agent.yaml").write_text("name: Not_Valid\n")
    (tmp_path / "settings.json").write_text("{}\n")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.md").write_text("---\nname: x\n---\n")
    errors = validate_plugin_dir(tmp_path)
    assert any("schema violation" in e for e in errors)


def test_validate_plugin_dir_agent_yaml_not_valid_yaml(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text("name: [unterminated\n")
    (tmp_path / "settings.json").write_text("{}\n")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.md").write_text("---\nname: x\n---\n")
    errors = validate_plugin_dir(tmp_path)
    assert any("not valid YAML" in e for e in errors)


def test_discover_plugin_configs_finds_test_plugin(fixture_workflows_dir: Path) -> None:
    configs = discover_plugin_configs(fixture_workflows_dir)
    names = [name for name, _ in configs]
    assert "platform-test" in names


def test_discover_plugin_configs_missing_dir(tmp_path: Path) -> None:
    assert discover_plugin_configs(tmp_path / "does-not-exist") == []


def test_discover_message_routes_maps_channel_to_workflow(fixture_workflows_dir: Path) -> None:
    routes = discover_message_routes(fixture_workflows_dir)
    assert routes.get("platform-test-channel") == "platform-test"


def test_discover_workflow_packages_accepts_repo_root_with_workflows(fixture_repo_root: Path) -> None:
    packages = discover_workflow_packages([fixture_repo_root])
    by_name = {package.name: package for package in packages}
    assert "platform-test" in by_name
    assert by_name["platform-test"].path == fixture_repo_root / "workflows" / "platform-test"


def test_discover_workflow_packages_scans_multiple_roots(tmp_path: Path, fixture_workflows_dir: Path) -> None:
    external_workflows = tmp_path / "external" / "workflows"
    workflow_dir = external_workflows / "external-workflow"
    (workflow_dir / "agents").mkdir(parents=True)
    (workflow_dir / "agents" / "external.md").write_text("---\nname: external\n---\n")
    (workflow_dir / "agent.yaml").write_text("name: external-workflow\nmessaging:\n  channels: [external]\n")
    (workflow_dir / "settings.json").write_text("{}\n")
    (workflow_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))

    packages = discover_workflow_packages([fixture_workflows_dir, tmp_path / "external"])
    names = {package.name for package in packages}
    assert {"platform-test", "external-workflow"}.issubset(names)


def test_read_plugin_files_includes_text_files(test_plugin_dir: Path) -> None:
    files = read_plugin_files(test_plugin_dir)
    assert "agent.yaml" in files
    assert "settings.json" in files
    assert ".mcp.json" in files
    assert any(name.startswith("agents/") for name in files)
