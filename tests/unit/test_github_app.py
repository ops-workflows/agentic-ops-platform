"""Unit tests for platform-owned GitHub App authentication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.lib import github_app, platform_secrets
from shared.lib.platform_secrets import GitHubAppConnection, load_github_app_connections, load_platform_env

pytestmark = pytest.mark.unit


def test_connection_loader_decrypts_key_without_exporting_it_to_platform_env(monkeypatch, tmp_path) -> None:
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """github:
    connections:
        github-public:
            web_base_url: https://github.com
            api_base_url: https://api.github.com
            app_id: '123'
            installation_id: '456'
            private_key_secret: github_public_app_private_key
secrets:
    github_public_app_private_key:
        encrypted: encrypted-value
    OTHER_SECRET:
        encrypted: other-value
""",
        encoding="utf-8",
    )

    def decrypt(secrets, *, identity):
        return {name: "private-key" if name == "github_public_app_private_key" else "other-secret" for name in secrets}

    monkeypatch.setattr(platform_secrets, "decrypt_named_secrets", decrypt)

    connection = load_github_app_connections(str(config), identity="age-identity")["github-public"]
    environment = load_platform_env(str(config), identity="age-identity")

    assert connection.private_key == "private-key"
    assert "github_public_app_private_key" not in environment
    assert environment["OTHER_SECRET"] == "other-secret"


def test_installation_token_uses_connection_api_and_cache(monkeypatch) -> None:
    connection = GitHubAppConnection(
        name="public",
        web_base_url="https://github.com",
        api_base_url="https://api.github.com",
        app_id="123",
        installation_id="456",
        private_key="private-key",
    )
    monkeypatch.setattr(github_app, "github_app_connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(github_app.jwt, "encode", lambda *args, **kwargs: "app-jwt")
    github_app._TOKEN_CACHE.clear()
    calls = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            expiry = datetime.now(UTC) + timedelta(hours=1)
            return {"token": "installation-token", "expires_at": expiry.isoformat()}

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url, *, headers):
            calls.append((url, headers))
            return Response()

    monkeypatch.setattr(github_app.httpx, "Client", Client)

    first = github_app.github_installation_token("public", platform_config_file="config.yaml", age_identity="identity")
    second = github_app.github_installation_token("public", platform_config_file="config.yaml", age_identity="identity")

    assert first == second == "installation-token"
    assert calls == [
        (
            "https://api.github.com/app/installations/456/access_tokens",
            {
                "Authorization": "Bearer app-jwt",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    ]


def test_installation_token_reports_invalid_private_key(monkeypatch) -> None:
    connection = GitHubAppConnection("public", "https://github.com", "https://api.github.com", "123", "456", "invalid")
    monkeypatch.setattr(github_app, "github_app_connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(
        github_app.jwt,
        "encode",
        lambda *args, **kwargs: (_ for _ in ()).throw(github_app.jwt.InvalidKeyError("invalid")),
    )
    github_app._TOKEN_CACHE.clear()

    with pytest.raises(github_app.GitHubAppError, match="invalid private key"):
        github_app.github_installation_token("public", platform_config_file="config.yaml", age_identity="identity")
