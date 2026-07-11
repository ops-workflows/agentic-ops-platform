"""Plugin directory helpers for Agentic Ops.

Plugins use a flat layout convention:

    agents/           — agent definitions (*.md with YAML frontmatter)
    skills/           — plugin-specific domain skills (*/SKILL.md)
    hooks/hooks.json  — hook event → command registrations
    settings.json     — Claude project settings (permissions, sandbox, agent)
    .mcp.json         — MCP server definitions
    agent.yaml        — platform config (secrets, schedules, messaging)

The runtime's _prepare_workspace() assembles the .claude/ project structure
expected by Claude Code from this flat layout at session start.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema
import yaml

from shared.lib.workflow_paths import discover_workflow_packages

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_YAML_SCHEMA_PATH = REPO_ROOT / "schemas" / "agent-yaml-schema.json"


@lru_cache(maxsize=1)
def _agent_yaml_schema() -> dict:
    return json.loads(AGENT_YAML_SCHEMA_PATH.read_text())


def read_platform_config(plugin_dir: Path) -> dict:
    """Read agent.yaml for platform-level config (secrets, schedules, runtime).

    Returns the parsed YAML config, or empty dict if not found.
    """
    agent_yaml_path = plugin_dir / "agent.yaml"
    if not agent_yaml_path.exists():
        return {}
    return yaml.safe_load(agent_yaml_path.read_text()) or {}


def discover_plugin_configs(plugins_dir: Path) -> list[tuple[str, dict]]:
    """Return discovered workflow configs from a plugins root directory."""
    return [(package.name, package.config) for package in discover_workflow_packages([plugins_dir])]


def discover_all_plugin_configs() -> list[tuple[str, dict]]:
    """Return workflow configs from all configured workflow repository roots."""
    return [(package.name, package.config) for package in discover_workflow_packages()]


def _messaging_config(config: dict) -> dict:
    messaging = config.get("messaging") or {}
    return messaging if isinstance(messaging, dict) else {}


def discover_message_routes(plugins_dir: Path) -> dict[str, str]:
    """Return channel_name -> workflow mappings from discovered workflows.

    Each workflow's agent.yaml lists the channels it owns under
    ``messaging.channels``. The gateway uses this map to resolve which workflow
    handles a message based on the channel name.
    """
    routes: dict[str, str] = {}

    for workflow, config in discover_plugin_configs(plugins_dir):
        messaging = _messaging_config(config)
        for channel in messaging.get("channels") or []:
            channel = str(channel).strip().lower()
            if channel:
                routes[channel] = workflow

    return routes


def discover_all_message_routes() -> dict[str, str]:
    routes: dict[str, str] = {}
    for package in discover_workflow_packages():
        messaging = _messaging_config(package.config)
        for channel in messaging.get("channels") or []:
            channel = str(channel).strip().lower()
            if channel:
                routes[channel] = package.name
    return routes


def validate_plugin_dir(plugin_dir: Path) -> list[str]:
    """Validate that a plugin directory has required files in flat layout."""
    errors: list[str] = []

    agent_yaml_path = plugin_dir / "agent.yaml"
    if not agent_yaml_path.exists():
        errors.append("Missing agent.yaml (platform config)")
    else:
        try:
            agent_config = yaml.safe_load(agent_yaml_path.read_text()) or {}
            jsonschema.validate(agent_config, _agent_yaml_schema())
        except yaml.YAMLError as exc:
            errors.append(f"agent.yaml is not valid YAML: {exc}")
        except jsonschema.ValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
            errors.append(f"agent.yaml schema violation at {location}: {exc.message}")

    if not (plugin_dir / ".mcp.json").exists():
        errors.append("Missing .mcp.json (MCP server config)")

    # CLAUDE.md is injected from shared/ at runtime — not required per-plugin

    # settings.json at root (not .claude/settings.json)
    if not (plugin_dir / "settings.json").exists():
        errors.append("Missing settings.json (Claude project settings)")

    # agents/ at root (flat layout)
    agents_dir = plugin_dir / "agents"
    if not (agents_dir.exists() and list(agents_dir.glob("*.md"))):
        errors.append("Missing agents/*.md — no agent definitions found")

    mcp_json_path = plugin_dir / ".mcp.json"
    if mcp_json_path.exists():
        try:
            config = json.loads(mcp_json_path.read_text())
            if "mcpServers" not in config:
                errors.append(".mcp.json missing 'mcpServers' key")
        except json.JSONDecodeError as exc:
            errors.append(f".mcp.json is not valid JSON: {exc}")

    hooks_json_path = plugin_dir / "hooks" / "hooks.json"
    if hooks_json_path.exists():
        try:
            json.loads(hooks_json_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"hooks/hooks.json is not valid JSON: {exc}")

    return errors


def read_plugin_files(plugin_dir: Path) -> dict[str, str]:
    """Read all text plugin files into a relative_path → content mapping."""
    files: dict[str, str] = {}

    for path in sorted(plugin_dir.rglob("*")):
        if path.is_file() and not path.name.startswith(".git"):
            rel_path = str(path.relative_to(plugin_dir))
            if path.suffix in (".py", ".md", ".json", ".yaml", ".yml", ".txt"):
                files[rel_path] = path.read_text()

    return files
