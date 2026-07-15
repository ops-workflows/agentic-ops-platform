"""Unit tests for workflow-repository PR proposal controls."""

from __future__ import annotations

import pytest

from mcps.core import mcp_platform

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("workflow_repo_url", "expected"),
    [
        ("https://github.com/acme/corp-workflows.git", "acme/corp-workflows"),
        ("https://github.com/acme/corp-workflows", "acme/corp-workflows"),
        ("git@github.com:acme/corp-workflows.git", "acme/corp-workflows"),
        ("https://git.example.internal/acme/corp-workflows.git", ""),
    ],
)
def test_workflow_github_repo_is_derived_only_from_bootstrap_url(monkeypatch, workflow_repo_url, expected):
    monkeypatch.setattr(mcp_platform, "WORKFLOW_REPO_URL", workflow_repo_url)
    assert mcp_platform._workflow_github_repo() == expected


@pytest.mark.parametrize(
    "file_path",
    [
        "skills/private-shared/SKILL.md",
        "workflows/incident-investigator/agents/coordinator.md",
        "workflows/incident-investigator/CLAUDE.md",
        "workflows/incident-investigator/skills/triage/SKILL.md",
    ],
)
def test_validate_skill_path_allows_only_private_workflow_repo_content(file_path):
    mcp_platform._validate_skill_path(file_path)


@pytest.mark.parametrize(
    "file_path",
    [
        "skills/reflect/README.md",
        "workflows/incident-investigator/README.md",
        "../skills/private-shared/SKILL.md",
        "CLAUDE.md",
    ],
)
def test_validate_skill_path_rejects_non_updatable_paths(file_path):
    with pytest.raises(ValueError, match="relative|platform-core files are read-only"):
        mcp_platform._validate_skill_path(file_path)
