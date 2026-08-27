"""Unit tests for optional Docker Compose profile derivation."""

from scripts.compose_profiles import compute_profiles


def test_compute_profiles_includes_github_mcp() -> None:
    config = {
        "mcps": {"enabled": ["message", "github", "splunk"]},
        "connectors": {"enabled": [], "instances": {}},
    }

    assert compute_profiles(config) == ["github", "splunk"]
