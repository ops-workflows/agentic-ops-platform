"""Unit tests for workflow-repo PAT-authenticated URL construction and the
bundle/platform compatibility policy.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_sync_platform_root_contains_shared_runtime_assets():
    from shared.lib import workflow_repo_sync as sync_mod

    platform_root = sync_mod._platform_root()

    assert platform_root == Path(sync_mod.__file__).resolve().parents[2]
    assert (platform_root / "skills" / "large-result-handling" / "SKILL.md").is_file()
    assert (platform_root / "hooks" / "auto_recall_hook.py").is_file()


def test_publish_platform_config_snapshot_uses_local_repo_config(monkeypatch, tmp_path):
    from shared.lib import workflow_repo_sync as sync_mod
    from shared.lib.config import settings

    source = tmp_path / "platform-config.yaml"
    source.write_text("default_model_profile: synced\n", encoding="utf-8")
    bundle_root = tmp_path / "bundles"
    monkeypatch.setattr(settings, "workflow_repo_url", "")
    monkeypatch.setattr(settings, "workflow_repo_source", "local")
    monkeypatch.setattr(settings, "platform_config_file", str(source))

    snapshot = sync_mod._publish_platform_config_snapshot(bundle_root)

    assert snapshot == bundle_root / "platform-config.yaml"
    assert snapshot.read_text(encoding="utf-8") == "default_model_profile: synced\n"


def test_publish_platform_config_snapshot_uses_checked_out_remote_repo(monkeypatch, tmp_path):
    from shared.lib import workflow_repo_sync as sync_mod
    from shared.lib.config import settings

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "platform-config.yaml").write_text("default_model_profile: pinned\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workflow_repo_url", "https://github.com/acme/workflows.git")
    monkeypatch.setattr(settings, "workflow_repo_source", "github")
    monkeypatch.setattr(settings, "workflow_repo_local_path", str(checkout))

    snapshot = sync_mod._publish_platform_config_snapshot(tmp_path / "bundles")

    assert snapshot.read_text(encoding="utf-8") == "default_model_profile: pinned\n"


def test_publish_object_store_release_advances_pointer_after_manifest(monkeypatch, tmp_path):
    from shared.lib import workflow_repo_sync as sync_mod

    config = tmp_path / "platform-config.yaml"
    config.write_text("default_model_profile: synced\n", encoding="utf-8")
    uploads: list[tuple[str, str, bytes]] = []
    monkeypatch.setattr(
        sync_mod,
        "upload_bytes",
        lambda bucket, key, data, *, content_type="application/octet-stream": uploads.append((bucket, key, data)),
    )

    sync_mod._publish_object_store_release(
        bucket="agentic-ops-bundles",
        release_id="commit-123",
        platform_config=config,
        bundles={"platform-test": {"key": "releases/commit-123/bundles/platform-test.tar.gz", "checksum": "sha256:x"}},
        commit="commit-123",
        effective_ref="v1.0.0",
    )

    assert [key for _, key, _ in uploads] == [
        "releases/commit-123/platform-config.yaml",
        "releases/commit-123/manifest.json",
        "releases/active.json",
    ]
    assert json.loads(uploads[-1][2]) == {"manifest_key": "releases/commit-123/manifest.json"}


def test_release_id_is_unique_for_repeated_syncs_at_the_same_commit(monkeypatch):
    from datetime import UTC, datetime

    from shared.lib import workflow_repo_sync as sync_mod

    timestamps = iter(
        [
            datetime(2026, 5, 8, 12, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 5, 8, 12, 0, 0, 2, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(sync_mod, "datetime", type("Clock", (), {"now": lambda tz: next(timestamps)}))

    first = sync_mod._release_id(commit="0123456789abcdef", effective_ref="main")
    second = sync_mod._release_id(commit="0123456789abcdef", effective_ref="main")

    assert first.startswith("0123456789ab-")
    assert first != second
