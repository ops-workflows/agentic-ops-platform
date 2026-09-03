"""Unit tests for workflow-repository PR proposal controls."""

from __future__ import annotations

import pytest

from mcps.core import mcp_platform
from shared.lib.github_app import GitHubAppError
from shared.lib.platform_secrets import GitHubAppConnection

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("workflow_repo_url", "connection", "expected"),
    [
        (
            "https://github.com/acme/corp-workflows.git",
            GitHubAppConnection("public", "https://github.com", "https://api.github.com", "1", "2", "key"),
            ("https://api.github.com", "acme", "corp-workflows", "installation-token"),
        ),
        (
            "https://github.company.example/acme/corp-workflows",
            GitHubAppConnection(
                "enterprise",
                "https://github.company.example",
                "https://github.company.example/api/v3",
                "3",
                "4",
                "key",
            ),
            ("https://github.company.example/api/v3", "acme", "corp-workflows", "installation-token"),
        ),
    ],
)
def test_workflow_github_context_uses_selected_connection(monkeypatch, workflow_repo_url, connection, expected):
    monkeypatch.setattr(mcp_platform, "WORKFLOW_REPO_URL", workflow_repo_url)
    monkeypatch.setattr(mcp_platform, "load_workflow_repo_github_connection", lambda path: connection.name)
    monkeypatch.setattr(mcp_platform, "github_app_connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(mcp_platform, "github_installation_token", lambda *args, **kwargs: "installation-token")
    assert mcp_platform._workflow_github_context() == expected


def test_workflow_github_context_rejects_repo_on_other_host(monkeypatch):
    connection = GitHubAppConnection("public", "https://github.com", "https://api.github.com", "1", "2", "key")
    monkeypatch.setattr(mcp_platform, "WORKFLOW_REPO_URL", "https://other.example/acme/workflows.git")
    monkeypatch.setattr(mcp_platform, "load_workflow_repo_github_connection", lambda path: connection.name)
    monkeypatch.setattr(mcp_platform, "github_app_connection", lambda *args, **kwargs: connection)
    with pytest.raises(GitHubAppError, match="does not belong"):
        mcp_platform._workflow_github_context()


@pytest.mark.parametrize(
    "file_path",
    [
        "skills/private-shared/SKILL.md",
        "workflows/example-workflow/agents/coordinator.md",
        "workflows/example-workflow/CLAUDE.md",
        "workflows/example-workflow/skills/triage/SKILL.md",
    ],
)
def test_validate_skill_path_allows_only_private_workflow_repo_content(file_path):
    mcp_platform._validate_skill_path(file_path)


@pytest.mark.parametrize(
    "file_path",
    [
        "skills/reflect/README.md",
        "workflows/example-workflow/README.md",
        "../skills/private-shared/SKILL.md",
        "CLAUDE.md",
    ],
)
def test_validate_skill_path_rejects_non_updatable_paths(file_path):
    with pytest.raises(ValueError, match="relative|platform-core files are read-only"):
        mcp_platform._validate_skill_path(file_path)
