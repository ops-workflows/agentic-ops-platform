from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shared.lib.workflow_bundles import WorkflowRepoMetadata, build_workflow_bundle

pytestmark = pytest.mark.unit


def test_build_workflow_bundle_merges_core_team_and_workflow_assets(
    repo_root: Path, fixture_repo_root: Path, tmp_path: Path
):
    team_repo = tmp_path / "team-repo"
    shutil.copytree(fixture_repo_root, team_repo)
    (team_repo / "skills" / "message-communication").mkdir(parents=True)
    (team_repo / "skills" / "message-communication" / "SKILL.md").write_text("team override\n", encoding="utf-8")
    (team_repo / "hooks" / "team_hook.py").parent.mkdir(parents=True, exist_ok=True)
    (team_repo / "hooks" / "team_hook.py").write_text("print('team')\n", encoding="utf-8")

    result = build_workflow_bundle(
        workflow="platform-test",
        output_dir=tmp_path / "bundles",
        platform_root=repo_root,
        workflow_roots=[team_repo],
        repo_metadata=WorkflowRepoMetadata(
            name="team", url="https://example.test/team.git", ref="main", commit="abc123"
        ),
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    bundle = result.bundle_dir
    assert (bundle / "manifest.yaml").exists()
    assert (bundle / "CLAUDE.md").exists()
    assert (bundle / "agent.yaml").exists()
    assert (bundle / "settings.json").exists()
    assert (bundle / ".mcp.json").exists()
    assert (bundle / "agents" / "test-coordinator.md").exists()
    assert (bundle / "hooks" / "auto_recall_hook.py").exists()
    assert (bundle / "hooks" / "test_prompt_hook.py").exists()
    assert (bundle / "hooks" / "team_hook.py").exists()
    assert (bundle / "skills" / "test-skill" / "SKILL.md").exists()
    team_skill = bundle / "skills" / "message-communication" / "SKILL.md"
    assert team_skill.read_text(encoding="utf-8") == "team override\n"
    assert result.manifest["workflow"]["name"] == "platform-test"
    assert result.manifest["repo"]["commit"] == "abc123"
    assert result.manifest["created_at"] == "2026-01-02T03:04:05Z"
    assert result.manifest["assets"]["sources"]["skills/message-communication/SKILL.md"] == "team"
    assert result.manifest["assets"]["sources"]["skills/reflect/SKILL.md"] == "core"
    assert result.manifest["assets"]["sources"]["agents/test-coordinator.md"] == "workflow"
    assert any(
        item["path"] == "skills/message-communication/SKILL.md" for item in result.manifest["assets"]["shadowed"]
    )


def test_build_workflow_bundle_rejects_plaintext_workflow_secret(
    repo_root: Path, fixture_repo_root: Path, tmp_path: Path
):
    team_repo = tmp_path / "team-repo"
    shutil.copytree(fixture_repo_root, team_repo)
    agent_yaml = team_repo / "workflows" / "platform-test" / "agent.yaml"
    agent_yaml.write_text(agent_yaml.read_text(encoding="utf-8") + "\n  BAD_TOKEN: plaintext-token\n", encoding="utf-8")

    with pytest.raises(ValueError, match="plaintext-looking secret"):
        build_workflow_bundle(
            workflow="platform-test",
            output_dir=tmp_path / "bundles",
            platform_root=repo_root,
            workflow_roots=[team_repo],
        )


def test_upload_bundle_archive_uploads_tarball_of_bundle_dir(monkeypatch, tmp_path: Path):
    from shared.lib.workflow_bundles import upload_bundle_archive

    bundle_dir = tmp_path / "platform-test"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.yaml").write_text("bundle_version: 1\n", encoding="utf-8")
    (bundle_dir / "CLAUDE.md").write_text("hello\n", encoding="utf-8")

    uploaded: dict[str, object] = {}

    def _fake_upload_bytes(bucket, key, data, *, content_type):
        uploaded["bucket"] = bucket
        uploaded["key"] = key
        uploaded["data"] = data
        uploaded["content_type"] = content_type
        return key

    import shared.lib.object_store as object_store_mod

    monkeypatch.setattr(object_store_mod, "upload_bytes", _fake_upload_bytes)

    key = upload_bundle_archive(bundle_dir, "platform-test", bucket="my-bundles")

    assert key == "bundles/platform-test.tar.gz"
    assert uploaded["bucket"] == "my-bundles"
    assert uploaded["key"] == "bundles/platform-test.tar.gz"
    assert uploaded["content_type"] == "application/gzip"

    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(uploaded["data"]), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "./manifest.yaml" in names
    assert "./CLAUDE.md" in names
