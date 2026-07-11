"""Unit tests for sandbox tool-pattern matching.

Tests ``_tool_matches_pattern`` from the runtime entrypoint which powers
the ``permissions.ask`` and ``permissions.deny`` gates. Patterns mirror
Claude Code's settings.json syntax.
"""

from __future__ import annotations

import os

# The runtime entrypoint reads these at import time.
os.environ.setdefault("TASK_ID", "test")
os.environ.setdefault("TASK_PROMPT", "test")

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

from runtime.session_entrypoint import _tool_matches_pattern  # noqa: E402

# ── Bash patterns ──────────────────────────────────────────


def test_bash_glob_matches():
    assert _tool_matches_pattern("Bash", {"command": "rm -rf /tmp/foo"}, "Bash(rm -rf *)")


def test_bash_glob_does_not_match_other_prefix():
    assert not _tool_matches_pattern("Bash", {"command": "ls /tmp"}, "Bash(rm -rf *)")


def test_bash_exact_prefix_wildcard():
    assert _tool_matches_pattern("Bash", {"command": "echo approval-needed xyz"}, "Bash(echo approval-needed *)")


def test_bash_pattern_does_not_match_other_tool():
    assert not _tool_matches_pattern("Write", {"command": "rm -rf /"}, "Bash(rm -rf *)")


# ── MCP patterns ──────────────────────────────────────────


def test_mcp_namespace_wildcard():
    assert _tool_matches_pattern("mcp__splunk__search", {}, "mcp__splunk__*")


def test_mcp_single_tool():
    assert _tool_matches_pattern("mcp__splunk__search", {}, "mcp__splunk__search")


def test_mcp_namespace_prefix_matches_all_tools():
    # "mcp__splunk" (no trailing __*) should match any tool under that server
    assert _tool_matches_pattern("mcp__splunk__search", {}, "mcp__splunk")
    assert _tool_matches_pattern("mcp__splunk", {}, "mcp__splunk")


def test_mcp_namespace_does_not_match_other_server():
    assert not _tool_matches_pattern("mcp__memory__recall", {}, "mcp__splunk__*")


# ── Read / Write patterns ──────────────────────────────────────────


def test_read_glob_matches_secret_path():
    assert _tool_matches_pattern("Read", {"file_path": "./secrets/api-key"}, "Read(./secrets/**)")


def test_read_exact_denies_env_file():
    assert _tool_matches_pattern("Read", {"file_path": "./.env"}, "Read(./.env)")


def test_write_wildcard_matches_any_file():
    assert _tool_matches_pattern("Write", {"file_path": "/tmp/out"}, "Write(*)")


# ── Edge cases ──────────────────────────────────────────


def test_exact_tool_name_match():
    assert _tool_matches_pattern("WebFetch", {}, "WebFetch")


def test_exact_tool_name_no_match():
    assert not _tool_matches_pattern("WebFetch", {}, "Grep")
