"""Load repo-stored platform config, runtime env, and encrypted secrets.

The file format mirrors `agent.yaml` by keeping clear-text config separate from
encrypted secrets, and adds a `runtime_env:` section for values that should be
injected into every ephemeral runtime container:

        message_bus:
            provider: mattermost
            api_url: https://message.example.com
        runtime_env:
            ANTHROPIC_MODEL: gemma4:26b
            DISABLE_TELEMETRY: true
        secrets:

All keys are env-style names so services can reuse their existing settings
without Docker Compose needing to decrypt or template secret values.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from shared.lib.crypto import decrypt_named_secrets

logger = logging.getLogger(__name__)

ENV_PLACEHOLDER_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
INLINE_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

MODEL_PROFILE_ENV_ALIASES: dict[str, str] = {}


@dataclass
class MessageBusConfig:
    """Provider configuration loaded exclusively from ``message_bus`` YAML."""

    provider: str = ""
    api_url: str = ""
    team_name: str = ""
    bot_token: str = ""
    app_token: str = ""
    action_callback_secret: str = ""


@dataclass(frozen=True)
class GitHubAppConnection:
    """One GitHub App installation used for a bounded set of repositories."""

    name: str
    web_base_url: str
    api_base_url: str
    app_id: str
    installation_id: str
    private_key: str


def load_github_app_connections(path: str, *, identity: str | None = None) -> dict[str, GitHubAppConnection]:
    """Load named GitHub App installations and decrypt only their referenced keys."""
    data = _read_platform_file(path)
    github = data.get("github")
    raw_connections = github.get("connections") if isinstance(github, dict) else None
    secrets = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
    if not isinstance(raw_connections, dict):
        return {}

    connections: dict[str, GitHubAppConnection] = {}
    for raw_name, raw_connection in raw_connections.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_connection, dict):
            continue
        private_key_secret = str(raw_connection.get("private_key_secret") or "").strip()
        private_key = ""
        if private_key_secret and identity:
            secret_spec = secrets.get(private_key_secret)
            if isinstance(secret_spec, dict):
                private_key = decrypt_named_secrets({private_key_secret: secret_spec}, identity=identity).get(
                    private_key_secret, ""
                )
            else:
                logger.warning(
                    "github.connections.%s references missing secret %r in %s",
                    name,
                    private_key_secret,
                    path,
                )
        connections[name] = GitHubAppConnection(
            name=name,
            web_base_url=str(raw_connection.get("web_base_url") or "").strip().rstrip("/"),
            api_base_url=str(raw_connection.get("api_base_url") or "").strip().rstrip("/"),
            app_id=str(raw_connection.get("app_id") or "").strip(),
            installation_id=str(raw_connection.get("installation_id") or "").strip(),
            private_key=private_key,
        )
    return connections


def load_workflow_repo_github_connection(path: str) -> str:
    """Return the connection selected for workflow-repository runtime operations."""
    data = _read_platform_file(path)
    github = data.get("github")
    if not isinstance(github, dict):
        return ""
    return str(github.get("workflow_repo_connection") or "").strip()


def load_platform_env(path: str, *, identity: str | None = None) -> dict[str, str]:
    """Load plain config and encrypted secrets from a repo file.

    Missing files are treated as "no repo config". The `config:` section can be
    loaded without an age identity. The `secrets:` section is decrypted only when
    an identity is available.
    """
    data = _read_platform_file(path)
    if not data:
        return {}

    env_values: dict[str, str] = {}
    config_values = data.get("config", {})
    if config_values:
        if isinstance(config_values, dict):
            env_values.update(_normalize_config_values(config_values, path=path))
        else:
            logger.warning("Platform config file has no valid 'config' mapping: %s", path)

    secret_values = data.get("secrets", {})
    if secret_values:
        if not isinstance(secret_values, dict):
            logger.warning("Platform config file has no valid 'secrets' mapping: %s", path)
        elif identity:
            excluded = _structured_secret_names(data)
            env_values.update(
                decrypt_named_secrets(
                    {name: spec for name, spec in secret_values.items() if str(name) not in excluded},
                    identity=identity,
                )
            )
        else:
            logger.info("Skipping encrypted platform secrets in %s because AGE_IDENTITY is not set", path)

    return env_values


def load_message_bus_config(path: str, *, identity: str | None = None) -> MessageBusConfig:
    """Load message provider settings from the native ``message_bus`` section.

    The returned configuration is never translated into environment variables;
    Compose and Kubernetes both consume the same mounted YAML file.
    """
    data = _read_platform_file(path)
    if not data:
        return MessageBusConfig()

    message_bus = data.get("message_bus")
    if not isinstance(message_bus, dict):
        logger.warning("Platform config file has no valid 'message_bus' mapping: %s", path)
        return MessageBusConfig()

    secrets = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
    return MessageBusConfig(
        provider=_message_bus_scalar(message_bus, "provider", path).lower(),
        api_url=_message_bus_scalar(message_bus, "api_url", path),
        team_name=_message_bus_scalar(message_bus, "team_name", path),
        bot_token=_message_bus_secret(message_bus, secrets, "bot_token_secret", identity, path),
        app_token=_message_bus_secret(message_bus, secrets, "app_token_secret", identity, path),
        action_callback_secret=_message_bus_secret(message_bus, secrets, "action_callback_secret", identity, path),
    )


def _message_bus_scalar(message_bus: dict[str, Any], key: str, path: str) -> str:
    value = message_bus.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        logger.warning("Skipping non-scalar message_bus.%s in %s", key, path)
        return ""
    return str(value).strip()


def _message_bus_secret(
    message_bus: dict[str, Any],
    secrets: dict[str, Any],
    reference_key: str,
    identity: str | None,
    path: str,
) -> str:
    secret_name = _message_bus_scalar(message_bus, reference_key, path)
    if not secret_name or not identity:
        return ""
    secret_spec = secrets.get(secret_name)
    if not isinstance(secret_spec, dict):
        logger.warning("message_bus.%s references missing secret %r in %s", reference_key, secret_name, path)
        return ""
    return decrypt_named_secrets({secret_name: secret_spec}, identity=identity).get(secret_name, "")


def _message_bus_secret_names(data: dict[str, Any]) -> set[str]:
    message_bus = data.get("message_bus")
    if not isinstance(message_bus, dict):
        return set()
    return {
        _message_bus_scalar(message_bus, reference_key, "platform-config.yaml")
        for reference_key in ("bot_token_secret", "app_token_secret", "action_callback_secret")
    } - {""}


def _github_app_secret_names(data: dict[str, Any]) -> set[str]:
    github = data.get("github")
    connections = github.get("connections") if isinstance(github, dict) else None
    if not isinstance(connections, dict):
        return set()
    return {
        str(connection.get("private_key_secret") or "").strip()
        for connection in connections.values()
        if isinstance(connection, dict)
    } - {""}


def _structured_secret_names(data: dict[str, Any]) -> set[str]:
    return _message_bus_secret_names(data) | _github_app_secret_names(data)


def load_platform_runtime_env(path: str, *, model_selector: str | None = None) -> dict[str, str | None]:
    """Load runtime-container env overrides from the repo config file.

    The top-level `runtime_env:` section is reserved for env vars that should be
    injected into every ephemeral runtime container. Scalar values are passed
    through as strings. `null` explicitly removes a variable from the runtime
    environment so built-in defaults can be disabled.
    """
    data = _read_platform_file(path)
    if not data:
        return {}

    runtime_values = data.get("runtime_env", {})
    normalized: dict[str, str | None] = {}
    if runtime_values:
        if not isinstance(runtime_values, dict):
            logger.warning("Platform config file has no valid 'runtime_env' mapping: %s", path)
            return {}
        normalized.update(_normalize_runtime_env_values(runtime_values, path=path))

    selector = (model_selector or str(data.get("default_model_profile") or "")).strip()
    if selector:
        normalized.update(_resolve_model_runtime_env(data, selector=selector, path=path))

    return normalized


def _resolve_model_runtime_env(data: dict[str, Any], *, selector: str, path: str) -> dict[str, str | None]:
    model_profiles = data.get("model_profiles", {})
    if model_profiles and not isinstance(model_profiles, dict):
        logger.warning("Platform config file has no valid 'model_profiles' mapping: %s", path)
        return _direct_model_override(selector)

    profile = model_profiles.get(selector) if isinstance(model_profiles, dict) else None
    if profile is None:
        return _direct_model_override(selector)

    if isinstance(profile, dict) and isinstance(profile.get("runtime_env"), dict):
        return _normalize_runtime_env_values(profile["runtime_env"], path=path)
    if isinstance(profile, dict):
        return _normalize_runtime_env_values(profile, path=path)

    logger.warning("Skipping non-mapping model profile %s in %s", selector, path)
    return {}


def _direct_model_override(model_name: str) -> dict[str, str]:
    return {
        "ANTHROPIC_MODEL": model_name,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model_name,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model_name,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_name,
        "CLAUDE_CODE_SUBAGENT_MODEL": model_name,
    }


def apply_platform_env_defaults(
    target_env: MutableMapping[str, str],
    *,
    path: str,
    identity: str | None = None,
) -> dict[str, str]:
    """Overlay repo config/secrets onto empty env vars in-place."""
    loaded = load_platform_env(path, identity=identity)
    for env_var, value in loaded.items():
        if target_env.get(env_var):
            continue
        target_env[env_var] = value
    return loaded


def expand_env_placeholders(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively expand ${VAR} placeholders in structured config using the environment."""
    source = env if env is not None else os.environ
    if isinstance(value, dict):
        return {key: expand_env_placeholders(item, source) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_placeholders(item, source) for item in value]
    if isinstance(value, str):
        return INLINE_ENV_PLACEHOLDER_PATTERN.sub(lambda match: source.get(match.group(1), ""), value)
    return value


def load_mcp_server_config(path: str, server_id: str) -> dict[str, Any]:
    """Return the structured config block for one MCP server from ``mcps.config.<server_id>``."""
    data = _read_platform_file(path)
    mcps = data.get("mcps")
    config = mcps.get("config") if isinstance(mcps, dict) else None
    server = config.get(server_id) if isinstance(config, dict) else None
    return expand_env_placeholders(server) if isinstance(server, dict) else {}


def load_connector_instances(path: str) -> dict[str, dict[str, Any]]:
    """Return every configured connector instance from ``connectors.instances``."""
    data = _read_platform_file(path)
    connectors = data.get("connectors")
    instances = connectors.get("instances") if isinstance(connectors, dict) else None
    if not isinstance(instances, dict):
        return {}
    return {
        str(instance_id): expand_env_placeholders(config)
        for instance_id, config in instances.items()
        if isinstance(config, dict)
    }


def load_connector_instance(path: str, instance_id: str) -> dict[str, Any]:
    """Return one connector instance definition by id, or an empty dict when absent."""
    return load_connector_instances(path).get(instance_id, {})


def load_enabled_connector_instance(path: str, connector_type: str) -> tuple[str, dict[str, Any]]:
    """Return the single enabled connector instance matching ``connector_type``.

    A deployment may still set ``CONNECTOR_INSTANCE_ID`` to select one instance
    explicitly. Without it, a generic connector can be configured entirely in
    ``platform-config.yaml`` when exactly one enabled instance uses its type.
    """
    data = _read_platform_file(path)
    connectors = data.get("connectors")
    if not isinstance(connectors, dict):
        return "", {}
    enabled = connectors.get("enabled")
    if not isinstance(enabled, list):
        return "", {}
    instances = load_connector_instances(path)
    matches = [
        (str(instance_id), instances[str(instance_id)])
        for instance_id in enabled
        if str(instance_id) in instances and instances[str(instance_id)].get("type") == connector_type
    ]
    if len(matches) == 1:
        return matches[0]
    return "", {}


def _read_platform_file(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError:
        logger.exception("Failed to read platform config file: %s", config_path)
        return {}
    except yaml.YAMLError:
        logger.exception("Failed to parse platform config file: %s", config_path)
        return {}

    if not isinstance(data, dict):
        logger.warning("Platform config file must be a YAML mapping: %s", config_path)
        return {}
    return data


def _normalize_config_values(config_values: dict[str, Any], *, path: str) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for env_var, value in config_values.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            normalized[str(env_var)] = _stringify_env_value(value, env_var=str(env_var), path=path)
            continue
        logger.warning("Skipping non-scalar platform config value for %s in %s", env_var, path)
    return normalized


def _normalize_runtime_env_values(runtime_values: dict[str, Any], *, path: str) -> dict[str, str | None]:
    normalized: dict[str, str | None] = {}
    for env_var, value in runtime_values.items():
        env_key = MODEL_PROFILE_ENV_ALIASES.get(str(env_var), str(env_var))
        if value is None:
            normalized[env_key] = None
            continue
        if isinstance(value, (str, int, float, bool)):
            normalized[env_key] = _stringify_env_value(value, env_var=env_key, path=path)
            continue
        logger.warning("Skipping non-scalar runtime_env value for %s in %s", env_var, path)
    return normalized


def _stringify_env_value(value: str | int | float | bool, *, env_var: str, path: str) -> str:
    if not isinstance(value, str):
        return str(value)

    match = ENV_PLACEHOLDER_PATTERN.match(value.strip())
    if not match:
        return value

    placeholder = match.group(1)
    resolved = os.environ.get(placeholder)
    if resolved is None:
        logger.warning("Environment placeholder %s for %s in %s is not set", placeholder, env_var, path)
        return ""
    return resolved
