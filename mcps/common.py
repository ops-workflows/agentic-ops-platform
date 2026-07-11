"""Shared helpers for HTTP MCP servers."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from urllib.parse import urlparse

from shared.lib.platform_secrets import apply_platform_env_defaults

logger = logging.getLogger(__name__)


def bootstrap_platform_env() -> None:
    """Load plain config and age-decrypted secrets into os.environ when missing."""
    platform_config_file = os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml")
    age_identity = os.environ.get("AGE_IDENTITY", "")
    try:
        apply_platform_env_defaults(
            os.environ,
            path=platform_config_file,
            identity=age_identity or None,
        )
    except Exception:
        logger.exception("Failed to load platform config from %s", platform_config_file)


def get_env(name: str, default: str = "") -> str:
    """Read an environment variable without triggering config side effects."""
    return os.environ.get(name, default)


def extract_bearer_token(headers: Mapping[str, str]) -> str:
    """Extract a bearer token from injected HTTP headers."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return ""


def require_header(headers: Mapping[str, str], header_name: str, description: str) -> str:
    """Return a required request header or raise a user-facing validation error."""
    value = headers.get(header_name, "").strip()
    if not value:
        raise ValueError(f"{description} must be provided via the {header_name} header")
    return value


def validate_base_url(value: str, *, header_name: str) -> str:
    """Validate that a request-scoped base URL is a concrete HTTP(S) origin."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{header_name} must use http or https")
    if not parsed.netloc:
        raise ValueError(f"{header_name} must include a host")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{header_name} must not include params, query strings, or fragments")
    normalized_path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
