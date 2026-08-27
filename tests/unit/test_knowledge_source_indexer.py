"""Strict Knowledge Source indexer helper tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.lib.knowledge_source_indexer import (
    KnowledgeSourceSyncError,
    _materialize_source_snapshot,
    _validated_git_ref,
    _validated_repository_url,
    extraction_config_hash,
    resolve_knowledge_source_credential,
)

pytestmark = pytest.mark.unit


def _source(**overrides):
    values = {
        "repository_url": "https://github.example.com/org/example.git",
        "include_paths": ["src/**"],
        "exclude_paths": ["src/generated/**"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_repository_identity_and_ref_are_strict() -> None:
    assert _validated_repository_url(_source()) == "https://github.example.com/org/example.git"
    assert _validated_git_ref("release/2026.08") == "release/2026.08"

    with pytest.raises(KnowledgeSourceSyncError, match="credential-free HTTPS"):
        _validated_repository_url(_source(repository_url="https://token@github.example.com/org/example.git"))
    with pytest.raises(KnowledgeSourceSyncError, match="Git ref syntax"):
        _validated_git_ref("../../main")


def test_credential_ref_uses_one_exact_github_connection(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.lib.knowledge_source_indexer.github_installation_token",
        lambda connection_name, **kwargs: f"token-for-{connection_name}",
    )
    assert resolve_knowledge_source_credential(None) == ""
    assert resolve_knowledge_source_credential("github-public") == "token-for-github-public"
    with pytest.raises(KnowledgeSourceSyncError, match="GitHub connection name"):
        resolve_knowledge_source_credential("secret/path")


def test_materialize_snapshot_enforces_scope_and_excludes_git_and_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "src" / "generated").mkdir(parents=True)
    (repository / ".git").mkdir()
    (repository / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    (repository / "src" / "generated" / "types.py").write_text("pass\n", encoding="utf-8")
    (repository / "README.md").write_text("outside\n", encoding="utf-8")
    (repository / ".git" / "config").write_text("credential\n", encoding="utf-8")
    (repository / "src" / "link.py").symlink_to(repository / "README.md")

    snapshot = tmp_path / "source"
    _materialize_source_snapshot(
        repository,
        snapshot,
        include_paths=["src/**"],
        exclude_paths=["src/generated/**"],
    )

    assert [path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()] == ["src/app.py"]


def test_extraction_hash_changes_only_with_extraction_contract() -> None:
    first = extraction_config_hash(_source())
    same = extraction_config_hash(_source(repository_url="https://github.example.com/renamed/example.git"))
    changed = extraction_config_hash(_source(include_paths=["services/**"]))

    assert first == same
    assert first != changed
