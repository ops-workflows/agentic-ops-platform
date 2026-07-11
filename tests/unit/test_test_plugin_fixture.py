"""Layer 0 — synthetic test plugin fixture sanity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_plugin_agent_yaml_has_expected_shape(test_plugin_dir: Path) -> None:
    cfg = yaml.safe_load((test_plugin_dir / "agent.yaml").read_text())
    assert cfg["name"] == "platform-test"
    assert cfg["runtime"]["container_image"] == "ai-ops-agent-runtime:latest"
    assert cfg["messaging"]["channels"] == ["platform-test-channel"]
    assert cfg["schedules"][0]["cron"] == "0 9 * * *"
    assert cfg["session"]["max_turns"] == 8


def test_plugin_settings_has_both_ask_and_deny(test_plugin_dir: Path) -> None:
    cfg = json.loads((test_plugin_dir / "settings.json").read_text())
    assert cfg["permissions"]["ask"], "fixture must exercise permissions.ask"
    assert cfg["permissions"]["deny"], "fixture must exercise permissions.deny"
    assert cfg["sandbox"]["enabled"] is True


def test_plugin_mcp_json_references_testserver(test_plugin_dir: Path) -> None:
    cfg = json.loads((test_plugin_dir / ".mcp.json").read_text())
    assert "testserver" in cfg["mcpServers"]
    headers = cfg["mcpServers"]["testserver"]["headers"]
    assert "${TASK_ID}" in headers.values()
    assert "${TEST_FIXED_VAR}" in headers.values()
    assert "${TEST_SECRET_VAR}" in headers.values()


def test_plugin_hooks_point_at_executable_python_files(test_plugin_dir: Path) -> None:
    hooks_cfg = json.loads((test_plugin_dir / "hooks" / "hooks.json").read_text())
    prompt_hooks = hooks_cfg["hooks"]["UserPromptSubmit"]
    sa_hooks = hooks_cfg["hooks"]["SubagentStop"]
    assert prompt_hooks and sa_hooks
    shared_hook_dirs = [
        test_plugin_dir.parents[1] / "hooks",
        test_plugin_dir.parents[4] / "hooks",
    ]
    for block in prompt_hooks + sa_hooks:
        for h in block["hooks"]:
            rel = h["command"].removeprefix("./")
            found_locally = (test_plugin_dir / rel).exists()
            found_shared = any((hook_dir / Path(rel).name).exists() for hook_dir in shared_hook_dirs)
            assert found_locally or found_shared, f"missing {h['command']}"


def test_plugin_coordinator_and_subagent_are_present(test_plugin_dir: Path) -> None:
    assert (test_plugin_dir / "agents" / "test-coordinator.md").exists()
    assert (test_plugin_dir / "agents" / "helper.md").exists()


def test_plugin_local_skill_marker_present(test_plugin_dir: Path) -> None:
    skill = (test_plugin_dir / "skills" / "test-skill" / "SKILL.md").read_text()
    assert "PLATFORM_TEST_PLUGIN_LOCAL_SKILL_MARKER" in skill
