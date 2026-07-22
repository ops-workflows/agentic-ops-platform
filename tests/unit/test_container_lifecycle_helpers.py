"""Unit tests for container-lifecycle helpers.

Tests pure helper functions from ``session_manager.container_lifecycle``:
env merging, selector resolution, stale-name cleanup logic. Mocks the
Docker client.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import docker.errors
import pytest
from session_manager.container_lifecycle import (
    _active_platform_config_file,
    _apply_runtime_env_overrides,
    _cleanup_stale_container_name,
    _get_agent_env_vars,
    _get_session_model_selector,
    _load_active_release,
    _resolve_plugin_mount,
    _resolve_workflow_bundle,
)

import shared.lib.object_store as object_store_mod
from shared.lib.config import settings

pytestmark = pytest.mark.unit


# ── env merging ──────────────────────────────────────────


def test_agent_env_vars_coerces_scalars_to_strings():
    cfg = {"env": {"A": 1, "B": True, "C": "value", "D": 3.14}}
    env = _get_agent_env_vars(cfg)
    assert env == {"A": "1", "B": "True", "C": "value", "D": "3.14"}


def test_agent_env_vars_skips_none_and_non_scalar():
    cfg = {"env": {"A": None, "B": {"nested": 1}, "C": [1, 2], "D": "ok"}}
    env = _get_agent_env_vars(cfg)
    assert env == {"D": "ok"}


def test_agent_env_vars_empty_on_missing_or_bad_section():
    assert _get_agent_env_vars({}) == {}
    assert _get_agent_env_vars({"env": "not-a-dict"}) == {}


def test_apply_runtime_env_overrides_null_removes_variable():
    env = {"ANTHROPIC_API_KEY": "secret", "KEEP_ME": "yes"}
    _apply_runtime_env_overrides(env, {"ANTHROPIC_API_KEY": None})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["KEEP_ME"] == "yes"


def test_apply_runtime_env_overrides_scalar_sets_value():
    env = {"FOO": "old"}
    _apply_runtime_env_overrides(env, {"FOO": "new", "BAR": "added"})
    assert env == {"FOO": "new", "BAR": "added"}


# ── session model selector ──────────────────────────────────────────


def test_session_model_selector_returns_value():
    assert _get_session_model_selector({"session": {"model": "test"}}) == "test"


def test_session_model_selector_returns_none_for_missing():
    assert _get_session_model_selector({}) is None
    assert _get_session_model_selector({"session": {}}) is None
    assert _get_session_model_selector({"session": {"model": "   "}}) is None
    assert _get_session_model_selector({"session": "not-a-dict"}) is None


def test_active_platform_config_prefers_synced_snapshot(monkeypatch, tmp_path):
    mounted_config = tmp_path / "mounted-platform-config.yaml"
    mounted_config.write_text("default_model_profile: mounted\n", encoding="utf-8")
    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    snapshot = bundle_root / "platform-config.yaml"
    snapshot.write_text("default_model_profile: synced\n", encoding="utf-8")
    monkeypatch.setattr(settings, "platform_config_file", str(mounted_config))
    monkeypatch.setattr(settings, "platform_secrets_file", "")
    monkeypatch.setattr(settings, "runtime_bundle_root", str(bundle_root))

    assert _active_platform_config_file() == str(snapshot)


def test_active_platform_config_falls_back_before_first_sync(monkeypatch, tmp_path):
    mounted_config = tmp_path / "mounted-platform-config.yaml"
    mounted_config.write_text("default_model_profile: mounted\n", encoding="utf-8")
    monkeypatch.setattr(settings, "platform_config_file", str(mounted_config))
    monkeypatch.setattr(settings, "platform_secrets_file", "")
    monkeypatch.setattr(settings, "runtime_bundle_root", str(tmp_path / "bundles"))

    assert _active_platform_config_file() == str(mounted_config)


def test_active_release_selects_matching_config_and_bundle(monkeypatch, tmp_path):
    manifest = {
        "release_id": "release-1",
        "platform_config_key": "releases/release-1/platform-config.yaml",
        "bundles": {
            "platform-test": {
                "key": "releases/release-1/bundles/platform-test.tar.gz",
                "checksum": "sha256:bundle-manifest",
            }
        },
    }
    objects = {
        "releases/active.json": json.dumps({"manifest_key": "releases/release-1/manifest.json"}).encode(),
        "releases/release-1/manifest.json": json.dumps(manifest).encode(),
        "releases/release-1/platform-config.yaml": b"default_model_profile: synced\n",
    }
    monkeypatch.setattr(settings, "runtime_bundle_object_store_bucket", "agentic-ops-bundles")
    monkeypatch.setattr(settings, "runtime_bundle_root", str(tmp_path))
    monkeypatch.setattr(object_store_mod, "download_bytes", lambda bucket, key: objects.get(key))
    monkeypatch.setattr(
        object_store_mod,
        "presigned_get_url",
        lambda bucket, key, *, expires_sec=3600: f"https://objects.test/{bucket}/{key}?exp={expires_sec}",
    )

    release = _load_active_release()
    assert release == manifest
    config_path = _active_platform_config_file(release)
    bundle_path, bundle_uri, checksum = _resolve_workflow_bundle("platform-test", release=release)

    assert Path(config_path).read_text(encoding="utf-8") == "default_model_profile: synced\n"
    assert bundle_path is None
    assert (
        bundle_uri
        == "https://objects.test/agentic-ops-bundles/releases/release-1/bundles/platform-test.tar.gz?exp=3600"
    )
    assert checksum == "sha256:bundle-manifest"


# ── stale container cleanup ──────────────────────────────────────────


def test_cleanup_stale_container_removes_exited():
    client = MagicMock()
    existing = MagicMock()
    existing.status = "exited"
    client.containers.get.return_value = existing

    _cleanup_stale_container_name(client, "session-abc-test")

    existing.remove.assert_called_once_with(force=True)


def test_cleanup_stale_container_is_noop_when_not_found():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")

    # Should not raise
    _cleanup_stale_container_name(client, "session-abc-test")


def test_cleanup_stale_container_refuses_running():
    client = MagicMock()
    running = MagicMock()
    running.status = "running"
    client.containers.get.return_value = running

    from docker.errors import DockerException

    with pytest.raises(DockerException):
        _cleanup_stale_container_name(client, "session-running")

    running.remove.assert_not_called()


def test_resolve_plugin_mount_prefers_host_repo_root(monkeypatch):
    monkeypatch.setattr(settings, "host_repo_root", "/host/repo")

    assert _resolve_plugin_mount("platform-test") == "/host/repo/workflows/platform-test"


def test_resolve_workflow_bundle_returns_configured_uri_without_local_root(monkeypatch):
    monkeypatch.setattr(settings, "runtime_bundle_root", "")
    monkeypatch.setattr(settings, "runtime_bundle_uri_template", "s3://bundles/{workflow}")

    bundle_path, bundle_uri, checksum = _resolve_workflow_bundle("platform-test")

    assert bundle_path is None
    assert bundle_uri == "s3://bundles/platform-test"
    assert checksum is None


def test_resolve_workflow_bundle_uses_existing_local_bundle(monkeypatch, tmp_path):
    bundle_dir = tmp_path / "platform-test"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.yaml").write_text("bundle_version: 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "runtime_bundle_root", str(tmp_path))
    monkeypatch.setattr(settings, "runtime_bundle_uri_template", "")

    bundle_path, bundle_uri, checksum = _resolve_workflow_bundle("platform-test")

    assert bundle_path == str(bundle_dir)
    assert bundle_uri == f"file://{bundle_dir}"
    assert checksum.startswith("sha256:")


def test_resolve_workflow_bundle_uploads_to_object_store_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "runtime_bundle_uri_template", "")
    monkeypatch.setattr(settings, "runtime_bundle_object_store_bucket", "agentic-ops-bundles")
    monkeypatch.setattr(settings, "runtime_bundle_presigned_url_expires_sec", 900)

    fresh_root = tmp_path / "fresh-root"
    monkeypatch.setattr(settings, "runtime_bundle_root", str(fresh_root))

    def _fake_build(**kwargs):
        output_dir = kwargs["output_dir"]
        built_dir = output_dir / kwargs["workflow"]
        built_dir.mkdir(parents=True, exist_ok=True)
        (built_dir / "manifest.yaml").write_text("bundle_version: 1\n", encoding="utf-8")

    monkeypatch.setattr("session_manager.container_lifecycle.build_workflow_bundle", _fake_build)

    import shared.lib.object_store as object_store_mod
    import shared.lib.workflow_bundles as workflow_bundles_mod

    upload_calls: list[tuple] = []
    presign_calls: list[tuple] = []

    def _fake_upload(dir_arg, workflow_arg, *, bucket, key_prefix="bundles"):
        upload_calls.append((dir_arg, workflow_arg, bucket, key_prefix))
        return f"{key_prefix}/{workflow_arg}.tar.gz"

    def _fake_presign(bucket, key, *, expires_sec=3600):
        presign_calls.append((bucket, key, expires_sec))
        return f"https://fake/{bucket}/{key}?exp={expires_sec}"

    monkeypatch.setattr(workflow_bundles_mod, "upload_bundle_archive", _fake_upload)
    monkeypatch.setattr(object_store_mod, "presigned_get_url", _fake_presign)

    bundle_path, bundle_uri, checksum = _resolve_workflow_bundle("platform-test")

    expected_bundle_dir = fresh_root / "platform-test"
    assert bundle_path == str(expected_bundle_dir)
    assert bundle_uri == "https://fake/agentic-ops-bundles/bundles/platform-test.tar.gz?exp=900"
    assert checksum.startswith("sha256:")
    assert upload_calls == [(expected_bundle_dir, "platform-test", "agentic-ops-bundles", "bundles")]
    assert presign_calls == [("agentic-ops-bundles", "bundles/platform-test.tar.gz", 900)]


def test_resolve_workflow_bundle_reuses_object_store_key_without_reupload(monkeypatch, tmp_path):
    bundle_dir = tmp_path / "platform-test"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.yaml").write_text("bundle_version: 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "runtime_bundle_root", str(tmp_path))
    monkeypatch.setattr(settings, "runtime_bundle_uri_template", "")
    monkeypatch.setattr(settings, "runtime_bundle_object_store_bucket", "agentic-ops-bundles")

    import shared.lib.object_store as object_store_mod
    import shared.lib.workflow_bundles as workflow_bundles_mod

    def _fail_upload(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("upload_bundle_archive should not run for an existing local bundle")

    presign_calls: list[tuple] = []

    def _fake_presign(bucket, key, *, expires_sec=3600):
        presign_calls.append((bucket, key, expires_sec))
        return f"https://fake/{bucket}/{key}"

    monkeypatch.setattr(workflow_bundles_mod, "upload_bundle_archive", _fail_upload)
    monkeypatch.setattr(object_store_mod, "presigned_get_url", _fake_presign)

    _, bundle_uri, _ = _resolve_workflow_bundle("platform-test")

    assert bundle_uri == "https://fake/agentic-ops-bundles/bundles/platform-test.tar.gz"
    assert presign_calls == [("agentic-ops-bundles", "bundles/platform-test.tar.gz", 3600)]
