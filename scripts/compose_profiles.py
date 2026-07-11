#!/usr/bin/env python3
"""Derive the docker-compose COMPOSE_PROFILES value from a platform-config.yaml.

Reads which optional MCP servers, connectors, and model backends an instance's
platform-config.yaml actually enables, and prints the matching comma-separated
COMPOSE_PROFILES value for deploy/docker-compose.yml -- so `docker compose up`
starts only what this instance needs instead of requiring the operator to
track profile names by hand.

Usage:
    python scripts/compose_profiles.py [--config PATH]

    COMPOSE_PROFILES="$(python scripts/compose_profiles.py)" \\
        docker compose -f deploy/docker-compose.yml up -d

`--config` defaults to $PLATFORM_CONFIG_FILE / $HOST_PLATFORM_CONFIG_FILE, or
the bundled example, matching deploy/docker-compose.yml's own default.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "examples" / "workflow-repo" / "platform-config.example.yaml"

# mcps.enabled name -> compose profile name. Only servers gated by a profile
# in deploy/docker-compose.yml belong here -- core servers (message, memory,
# platform) always run and have no profile to enable.
MCP_PROFILES = {
    "salesforce": "salesforce",
    "splunk": "splunk",
    "cloudwatch": "cloudwatch",
    "jira": "jira",
}

# connectors.instances.<id>.type -> compose profile name.
CONNECTOR_PROFILES = {
    "gcp-pubsub": "gcp-pubsub",
    "servicenow": "servicenow",
}

# A substring found in any configured model_profiles.*.ANTHROPIC_BASE_URL ->
# the compose profile that backend needs. Every configured model profile is
# checked (not just default_model_profile), since a workflow can select any
# of them via session.model at runtime.
MODEL_BACKEND_PROFILES = {
    "model-gateway": "model-gateway",
    "local-llm": "local-llm",
}


def _config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_value = os.environ.get("PLATFORM_CONFIG_FILE") or os.environ.get("HOST_PLATFORM_CONFIG_FILE")
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG


def compute_profiles(config: dict) -> list[str]:
    profiles: set[str] = set()

    for server_id in (config.get("mcps") or {}).get("enabled") or []:
        if server_id in MCP_PROFILES:
            profiles.add(MCP_PROFILES[server_id])

    connectors = config.get("connectors") or {}
    instances = connectors.get("instances") or {}
    for instance_id in connectors.get("enabled") or []:
        instance = instances.get(instance_id) or {}
        connector_type = instance.get("type")
        if connector_type in CONNECTOR_PROFILES:
            profiles.add(CONNECTOR_PROFILES[connector_type])

    for profile in (config.get("model_profiles") or {}).values():
        base_url = str((profile or {}).get("ANTHROPIC_BASE_URL") or "")
        for needle, profile_name in MODEL_BACKEND_PROFILES.items():
            if needle in base_url:
                profiles.add(profile_name)

    return sorted(profiles)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--config", help="Path to platform-config.yaml (default: $PLATFORM_CONFIG_FILE or the bundled example)"
    )
    args = parser.parse_args()

    path = _config_path(args.config)
    if not path.exists():
        print(f"warning: {path} not found, no optional profiles enabled", file=sys.stderr)
        print("")
        return 0

    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    print(",".join(compute_profiles(config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
