"""GitHub App installation authentication shared by platform services."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jwt

from shared.lib.platform_secrets import GitHubAppConnection, load_github_app_connections


class GitHubAppError(RuntimeError):
    """Raised when a configured GitHub App connection cannot authenticate."""


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: float


_TOKEN_CACHE: dict[tuple[str, str, str, str], _CachedToken] = {}
_TOKEN_CACHE_LOCK = threading.Lock()


def github_app_connection(
    connection_name: str,
    *,
    platform_config_file: str,
    age_identity: str | None,
) -> GitHubAppConnection:
    """Resolve and validate one named GitHub App connection."""
    connection = load_github_app_connections(platform_config_file, identity=age_identity).get(connection_name)
    if connection is None:
        raise GitHubAppError(f"GitHub App connection {connection_name!r} is not configured")
    for field_name in ("web_base_url", "api_base_url", "app_id", "installation_id", "private_key"):
        if not getattr(connection, field_name):
            raise GitHubAppError(f"GitHub App connection {connection_name!r} is missing {field_name}")
    for field_name in ("web_base_url", "api_base_url"):
        parsed = urlparse(getattr(connection, field_name))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise GitHubAppError(f"GitHub App connection {connection_name!r} has an invalid {field_name}")
    return connection


def github_installation_token(
    connection_name: str,
    *,
    platform_config_file: str,
    age_identity: str | None,
    now: float | None = None,
) -> str:
    """Mint and cache a short-lived token for one GitHub App installation."""
    connection = github_app_connection(
        connection_name,
        platform_config_file=platform_config_file,
        age_identity=age_identity,
    )
    current_time = time.time() if now is None else now
    cache_key = (
        connection.api_base_url,
        connection.app_id,
        connection.installation_id,
        connection.private_key,
    )
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached.expires_at - current_time > 300:
            return cached.value

    try:
        app_jwt = jwt.encode(
            {
                "iat": int(current_time) - 60,
                "exp": int(current_time) + 540,
                "iss": connection.app_id,
            },
            connection.private_key,
            algorithm="RS256",
        )
    except (TypeError, ValueError, jwt.PyJWTError) as exc:
        raise GitHubAppError(f"GitHub App connection {connection_name!r} has an invalid private key") from exc
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{connection.api_base_url}/app/installations/{connection.installation_id}/access_tokens",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GitHubAppError(f"Failed to authenticate GitHub App connection {connection_name!r}: {exc}") from exc

    token = str(payload.get("token") or "") if isinstance(payload, dict) else ""
    expires_at_text = str(payload.get("expires_at") or "") if isinstance(payload, dict) else ""
    if not token or not expires_at_text:
        raise GitHubAppError(f"GitHub App connection {connection_name!r} returned an invalid installation token")
    try:
        expires_at = datetime.fromisoformat(expires_at_text.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError as exc:
        raise GitHubAppError(f"GitHub App connection {connection_name!r} returned an invalid token expiry") from exc

    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE[cache_key] = _CachedToken(token, expires_at)
    return token


def github_repository_coordinates(repository_url: str, connection: GitHubAppConnection) -> tuple[str, str]:
    """Return owner/repository only when the URL belongs to the connection's web origin."""
    repository = urlparse(repository_url.strip())
    web_origin = urlparse(connection.web_base_url)
    if (
        repository.scheme != "https"
        or repository.hostname != web_origin.hostname
        or repository.port != web_origin.port
        or repository.username
        or repository.password
        or repository.query
        or repository.fragment
    ):
        raise GitHubAppError("Repository URL does not belong to the selected GitHub App connection")
    prefix = web_origin.path.rstrip("/")
    path = repository.path
    if prefix and not path.startswith(f"{prefix}/"):
        raise GitHubAppError("Repository URL does not belong to the selected GitHub App connection")
    relative_path = path[len(prefix) :].strip("/").removesuffix(".git")
    parts = relative_path.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubAppError("Repository URL must identify one owner and repository")
    return parts[0], parts[1]


@contextmanager
def github_git_auth_environment(token: str) -> Iterator[Mapping[str, str]]:
    """Expose a short-lived installation token to Git without persisting it in the remote URL."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if not token:
        yield environment
        return
    with tempfile.NamedTemporaryFile("w", prefix="github-app-askpass-", delete=False) as script:
        script.write(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *Username*) printf '%s\\n' \"x-access-token\" ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_APP_INSTALLATION_TOKEN\" ;;\n"
            "esac\n"
        )
        script_path = Path(script.name)
    script_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    environment["GIT_ASKPASS"] = str(script_path)
    environment["GITHUB_APP_INSTALLATION_TOKEN"] = token
    try:
        yield environment
    finally:
        script_path.unlink(missing_ok=True)
