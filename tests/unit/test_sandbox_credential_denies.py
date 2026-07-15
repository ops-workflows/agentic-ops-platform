"""Unit tests for the sandbox credential deny-list generator.

Covers ``_build_credential_envvars`` from the runtime entrypoint — the pure
merge logic that turns declared secrets into ``sandbox.credentials.envVars``
deny entries. This runs host-independently (no sandbox / Docker required), so
the foolproof generation logic stays covered even on hosts where the
end-to-end enforcement scenario test cannot run (e.g. bubblewrap unavailable).
"""

from __future__ import annotations

import json
import os

# The runtime entrypoint reads these at import time.
os.environ.setdefault("TASK_ID", "test")
os.environ.setdefault("TASK_PROMPT", "test")

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

from runtime import session_entrypoint  # noqa: E402
from runtime.session_entrypoint import (  # noqa: E402
    _build_credential_envvars,
    _resolve_default_agent,
)


def test_discovered_secrets_become_sorted_deny_entries():
    out = _build_credential_envvars(
        discovered_names={"CRM_TOKEN", "JIRA_TOKEN", "MESSAGE_BUS_BOT_TOKEN"},
        existing_entries=[],
    )
    assert out == [
        {"name": "CRM_TOKEN", "mode": "deny"},
        {"name": "JIRA_TOKEN", "mode": "deny"},
        {"name": "MESSAGE_BUS_BOT_TOKEN", "mode": "deny"},
    ]


def test_example_placeholder_names_are_stripped():
    out = _build_credential_envvars(
        discovered_names={"EXAMPLE_EXTRA_SECRET"},
        existing_entries=[{"name": "EXAMPLE_EXTRA_SECRET", "mode": "deny"}],
    )
    assert out == []


def test_settings_seed_extra_var_is_kept_with_its_mode():
    out = _build_credential_envvars(
        discovered_names={"CRM_TOKEN"},
        existing_entries=[{"name": "EXTRA_VAR", "mode": "mask"}],
    )
    by_name = {entry["name"]: entry["mode"] for entry in out}
    assert by_name == {"CRM_TOKEN": "deny", "EXTRA_VAR": "mask"}


def test_existing_entry_mode_wins_over_discovered_default():
    out = _build_credential_envvars(
        discovered_names={"CRM_TOKEN"},
        existing_entries=[{"name": "CRM_TOKEN", "mode": "mask"}],
    )
    assert out == [{"name": "CRM_TOKEN", "mode": "mask"}]


def test_malformed_entries_are_ignored():
    out = _build_credential_envvars(
        discovered_names={"GOOD_TOKEN"},
        existing_entries=["not-a-dict", {"mode": "deny"}, {"name": ""}],  # type: ignore[list-item]
    )
    assert out == [{"name": "GOOD_TOKEN", "mode": "deny"}]


@pytest.mark.asyncio
async def test_run_agent_session_passes_project_mcp_config_and_cli_path_to_sdk(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "external_crm": {"type": "http", "url": "http://mcp-external-crm:8102/mcp"},
                    "jira": {"type": "http", "url": "http://mcp-jira:8106/mcp"},
                }
            }
        )
    )
    claude_settings_path = tmp_path / ".claude" / "settings.json"
    claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
    claude_settings_path.write_text(json.dumps({"agent": "platform-test-coordinator"}))

    monkeypatch.setattr(session_entrypoint, "PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(session_entrypoint, "CLAUDE_SETTINGS_PATH", claude_settings_path)
    monkeypatch.setattr(session_entrypoint, "MAX_TURNS", 1)
    monkeypatch.setattr(
        session_entrypoint.shutil,
        "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )

    captured: dict[str, object] = {}

    async def fake_query(*, prompt, options, resume=None):
        captured["mcp_servers"] = options.mcp_servers
        captured["cwd"] = options.cwd
        captured["cli_path"] = options.cli_path
        captured["extra_args"] = dict(options.extra_args)
        if False:
            yield None

    async def fake_report_event(*args, **kwargs):
        return None

    async def fake_can_use_tool(*args, **kwargs):
        return None

    monkeypatch.setattr(session_entrypoint, "query", fake_query)
    monkeypatch.setattr(session_entrypoint, "report_event", fake_report_event)
    monkeypatch.setattr(
        session_entrypoint,
        "build_can_use_tool",
        lambda *args, **kwargs: fake_can_use_tool,
    )

    await session_entrypoint.run_agent_session({}, {})

    assert captured == {
        "mcp_servers": {
            "mcpServers": {
                "external_crm": {"type": "http", "url": "http://mcp-external-crm:8102/mcp"},
                "jira": {"type": "http", "url": "http://mcp-jira:8106/mcp"},
            }
        },
        "cwd": str(tmp_path),
        "cli_path": "/usr/local/bin/claude",
        "extra_args": {"agent": "platform-test-coordinator"},
    }


def test_resolve_default_agent_reads_agent_from_settings(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"agent": "platform-test-coordinator"}))
    monkeypatch.setattr(session_entrypoint, "CLAUDE_SETTINGS_PATH", settings_path)
    assert _resolve_default_agent() == "platform-test-coordinator"


def test_resolve_default_agent_none_when_absent(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"permissions": {"ask": []}}))
    monkeypatch.setattr(session_entrypoint, "CLAUDE_SETTINGS_PATH", settings_path)
    assert _resolve_default_agent() is None
