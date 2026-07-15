"""Load repo-stored platform config, runtime env, and encrypted secrets.

The file format mirrors `agent.yaml` by keeping clear-text config separate from
encrypted secrets, and adds a `runtime_env:` section for values that should be
injected into every ephemeral runtime container:

        config:
            MESSAGE_BUS_API_URL: https://message.example.com
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
from pathlib import Path
from typing import Any

import yaml

from shared.lib.crypto import decrypt_named_secrets

logger = logging.getLogger(__name__)

ENV_PLACEHOLDER_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
INLINE_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

MODEL_PROFILE_ENV_ALIASES: dict[str, str] = {}


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
            env_values.update(decrypt_named_secrets(secret_values, identity=identity))
        else:
            logger.info("Skipping encrypted platform secrets in %s because AGE_IDENTITY is not set", path)

    message_bus = data.get("message_bus", {})
    if isinstance(message_bus, dict):
        provider = str(message_bus.get("provider") or "").strip()
        if provider:
            env_values.setdefault("MESSAGE_BUS_PROVIDER", provider)
    elif message_bus:
        logger.warning("Platform config file has no valid 'message_bus' mapping: %s", path)

    return env_values


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


def load_platform_secret_env(path: str, *, identity: str) -> dict[str, str]:
    """Backward-compatible wrapper for callers expecting repo secret values."""
    return load_platform_env(path, identity=identity)


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
