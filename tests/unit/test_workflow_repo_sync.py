"""Unit tests for workflow-repo PAT-authenticated URL construction and the
bundle/platform compatibility policy.
"""

from __future__ import annotations

import pytest

from shared.lib.workflow_paths import _authenticated_repo_url
from shared.lib.workflow_repo_sync import (
    COMPATIBILITY_ERROR,
    COMPATIBILITY_OK,
    COMPATIBILITY_WARNING,
    check_bundle_compatibility,
)

pytestmark = pytest.mark.unit


def test_authenticated_repo_url_injects_token_for_https():
    url = _authenticated_repo_url("https://github.com/acme/workflows.git", "ghp_secrettoken")
    assert url == "https://ghp_secrettoken@github.com/acme/workflows.git"


def test_authenticated_repo_url_returns_unchanged_without_pat():
    url = "https://github.com/acme/workflows.git"
    assert _authenticated_repo_url(url, "") == url


def test_authenticated_repo_url_does_not_rewrite_ssh_urls():
    url = "git@github.com:acme/workflows.git"
    assert _authenticated_repo_url(url, "ghp_secrettoken") == url


def test_authenticated_repo_url_does_not_overwrite_existing_credentials():
    url = "https://existing-user@github.com/acme/workflows.git"
    assert _authenticated_repo_url(url, "ghp_secrettoken") == url


# ── check_bundle_compatibility ───────────────────────────────────────


def test_same_major_version_is_ok():
    assert check_bundle_compatibility("1.4.0", "1.9.2") == COMPATIBILITY_OK


def test_bundle_major_newer_than_platform_is_incompatible():
    assert check_bundle_compatibility("2.0.0", "1.9.2") == COMPATIBILITY_ERROR


def test_bundle_major_older_than_platform_is_a_warning():
    assert check_bundle_compatibility("0.9.0", "1.0.0") == COMPATIBILITY_WARNING


def test_unparseable_versions_are_treated_as_ok():
    assert check_bundle_compatibility("unknown", "1.0.0") == COMPATIBILITY_OK
    assert check_bundle_compatibility("1.0.0", "unknown") == COMPATIBILITY_OK
