"""Unit tests for model_profiles resolution in platform_secrets.py.

Validates that:
- The test profile sets ANTHROPIC_BASE_URL correctly
- The default_model_profile fallback works
- An unknown profile falls back to direct model override
- null runtime_env values produce None (removal markers)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.lib.platform_secrets import load_platform_runtime_env

FIXTURE_PLATFORM_CONFIG = (
    Path(__file__).resolve().parents[1] / "fixtures" / "repo-root" / "shared" / "platform-config.yaml"
)

pytestmark = pytest.mark.unit


def test_test_profile_sets_anthropic_base_url():
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector="test")
    assert "ANTHROPIC_BASE_URL" in env
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:19999"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-token"  # noqa: S105 - test fixture value.
    assert env["ANTHROPIC_MODEL"] == "test-model"


def test_local_profile_sets_anthropic_base_url():
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector="local")
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:19999"


def test_default_profile_fallback():
    """When no selector is given, default_model_profile ('test') is used."""
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector=None)
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:19999"
    assert env["ANTHROPIC_MODEL"] == "test-model"


def test_unknown_profile_becomes_direct_model_name():
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector="claude-sonnet-4-20250514")
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-20250514"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-sonnet-4-20250514"
    # Should NOT contain ANTHROPIC_BASE_URL from a profile
    assert "ANTHROPIC_BASE_URL" not in env


def test_null_runtime_env_produces_none():
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector="test")
    # ANTHROPIC_API_KEY is set to null in runtime_env
    assert env.get("ANTHROPIC_API_KEY") is None
    assert "ANTHROPIC_API_KEY" in env  # key present with None value


def test_runtime_env_scalars_are_strings():
    env = load_platform_runtime_env(str(FIXTURE_PLATFORM_CONFIG), model_selector="test")
    assert env["DISABLE_TELEMETRY"] == "True"


def test_env_placeholders_expand_from_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_KEY", "omlx-test-token")
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
model_profiles:
  local:
    ANTHROPIC_BASE_URL: http://local-llm:8000
    ANTHROPIC_AUTH_TOKEN: ${LOCAL_LLM_KEY}
""".strip()
    )

    env = load_platform_runtime_env(str(config), model_selector="local")
    assert env["ANTHROPIC_BASE_URL"] == "http://local-llm:8000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "omlx-test-token"  # noqa: S105 - test fixture value.
