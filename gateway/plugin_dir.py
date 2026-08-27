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
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import jsonschema
import yaml

from shared.lib.workflow_paths import discover_workflow_packages

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_YAML_SCHEMA_PATH = REPO_ROOT / "schemas" / "agent-yaml-schema.json"


@dataclass(frozen=True)
class MessageRoute:
    """A workflow-owned rule for turning a human message into a task."""

    workflow: str
    channel: str
    trigger_words: tuple[str, ...] = ()
    rule_id: str = ""
    provider: str = ""
    priority: int = 0
    match_type: str = "prefix"
    pattern: str = ""
    trusted_user_ids: tuple[str, ...] = ()
    trusted_webhook_ids: tuple[str, ...] = ()
    trusted_webhook_names: tuple[str, ...] = ()
    allow_bot: bool = False
    root_posts_only: bool = False
    preserve_full_body: bool = False
    allowed_accounts: tuple[str, ...] = ()
    allowed_environments: tuple[str, ...] = ()
    coalesce_window_sec: int = 300


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


def _message_routes(workflows: list[tuple[str, dict]]) -> list[MessageRoute]:
    routes: list[MessageRoute] = []
    for workflow, config in workflows:
        messaging = _messaging_config(config)
        runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
        coalesce_window_sec = int(runtime.get("alert_coalesce_window_sec", 300))
        triggers = tuple(
            trigger
            for raw_trigger in messaging.get("trigger_words", ["@agent"])
            if (trigger := str(raw_trigger).strip().lower())
        )
        for raw_channel in messaging.get("channels") or []:
            channel = str(raw_channel).strip().lower()
            if channel and triggers:
                routes.append(
                    MessageRoute(
                        workflow=workflow,
                        channel=channel,
                        trigger_words=triggers,
                        coalesce_window_sec=coalesce_window_sec,
                    )
                )
        for raw_rule in messaging.get("rules") or []:
            if not isinstance(raw_rule, dict) or raw_rule.get("enabled", True) is False:
                continue
            rule_id = str(raw_rule.get("id") or "").strip()
            match = raw_rule.get("match") if isinstance(raw_rule.get("match"), dict) else {}
            match_type = str(match.get("type") or "").strip().lower()
            pattern = str(match.get("pattern") or "").strip()
            if not rule_id or match_type not in {"prefix", "regex"} or not pattern:
                raise ValueError(f"Workflow {workflow!r} has an invalid messaging rule")
            if len(pattern) > 256:
                raise ValueError(f"Workflow {workflow!r} messaging rule {rule_id!r} pattern is too long")
            if match_type == "regex":
                if any(token in pattern for token in ("(", ")", "{", "}", "|", "+")) or pattern.count("*") > 8:
                    raise ValueError(f"Workflow {workflow!r} messaging rule {rule_id!r} uses unsupported regex syntax")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"Workflow {workflow!r} messaging rule {rule_id!r} has invalid regex: {exc}"
                    ) from exc

            trusted = raw_rule.get("trusted_senders")
            trusted = trusted if isinstance(trusted, dict) else {}
            constraints = raw_rule.get("constraints")
            constraints = constraints if isinstance(constraints, dict) else {}
            rule_channels = raw_rule.get("channels") or messaging.get("channels") or []
            for raw_channel in rule_channels:
                channel = str(raw_channel).strip().lower()
                if not channel:
                    continue
                routes.append(
                    MessageRoute(
                        workflow=workflow,
                        channel=channel,
                        rule_id=rule_id,
                        provider=str(raw_rule.get("provider") or "").strip().lower(),
                        priority=int(raw_rule.get("priority") or 0),
                        match_type=match_type,
                        pattern=pattern,
                        trusted_user_ids=tuple(
                            str(value).strip() for value in trusted.get("user_ids") or [] if str(value).strip()
                        ),
                        trusted_webhook_ids=tuple(
                            str(value).strip() for value in trusted.get("webhook_ids") or [] if str(value).strip()
                        ),
                        trusted_webhook_names=tuple(
                            str(value).strip() for value in trusted.get("webhook_names") or [] if str(value).strip()
                        ),
                        allow_bot=bool(raw_rule.get("allow_bot", False)),
                        root_posts_only=bool(raw_rule.get("root_posts_only", False)),
                        preserve_full_body=bool(raw_rule.get("preserve_full_body", False)),
                        allowed_accounts=tuple(
                            str(value).strip() for value in constraints.get("accounts") or [] if str(value).strip()
                        ),
                        allowed_environments=tuple(
                            str(value).strip().lower()
                            for value in constraints.get("environments") or []
                            if str(value).strip()
                        ),
                        coalesce_window_sec=coalesce_window_sec,
                    )
                )
    return routes


def discover_message_routes(plugins_dir: Path) -> list[MessageRoute]:
    """Return workflow-owned channel and trigger rules from agent.yaml files."""
    return _message_routes(discover_plugin_configs(plugins_dir))


def discover_all_message_routes() -> list[MessageRoute]:
    return _message_routes([(package.name, package.config) for package in discover_workflow_packages()])


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
