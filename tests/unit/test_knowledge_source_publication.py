"""Immutable Knowledge Source object publication tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shared.lib.knowledge_source_graphify import build_knowledge_source_bundle, validate_graphify_output
from shared.lib.knowledge_source_publication import (
    KnowledgeSourcePublicationError,
    publish_knowledge_source_bundle,
)

pytestmark = pytest.mark.unit


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploaded: list[str] = []

    def upload_file(self, bucket: str, key: str, file_path: str) -> str:
        destination = self.root / bucket / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, destination)
        self.uploaded.append(key)
        return key

    def download_file(self, bucket: str, key: str, file_path: str) -> bool:
        source = self.root / bucket / key
        if not source.is_file():
            return False
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, file_path)
        return True


def _bundle(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    graphify_dir = tmp_path / "working" / "graphify-out"
    graphify_dir.mkdir(parents=True)
    (graphify_dir / "graph.json").write_text(
        '{"nodes":[{"id":"module:app","source_file":"app.py"}],"links":[],"hyperedges":[]}',
        encoding="utf-8",
    )
    (graphify_dir / "manifest.json").write_text(
        '{"app.py":{"ast_hash":"abc","semantic_hash":""}}',
        encoding="utf-8",
    )
    (graphify_dir / "GRAPH_REPORT.md").write_text("# Report\n", encoding="utf-8")
    extraction = validate_graphify_output(graphify_dir)
    return build_knowledge_source_bundle(
        source_id="4c199419-7a87-4e3c-93e8-411e7039a771",
        canonical_alias="org/example",
        repository_url="https://github.example.com/org/example.git",
        commit_sha="a" * 40,
        source_dir=source_dir,
        graphify=extraction,
        bundle_dir=tmp_path / "bundle",
        include_paths=[],
        exclude_paths=[],
        extraction_config_hash="b" * 64,
    )


def test_publish_uploads_manifest_last_and_verifies_all_objects(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")

    result = publish_knowledge_source_bundle(bundle, bucket="knowledge", object_store=store)

    prefix = "knowledge-sources/4c199419-7a87-4e3c-93e8-411e7039a771/" + "a" * 40 + "/" + "b" * 64
    assert store.uploaded == [
        f"{prefix}/graph.json",
        f"{prefix}/source.tar.zst",
        f"{prefix}/graph-report.md",
        f"{prefix}/extraction.log",
        f"{prefix}/manifest.json",
    ]
    assert result.artifact_keys["manifest"] == f"{prefix}/manifest.json"
    assert set(result.artifact_checksums) == {"graph", "source", "report", "extraction_log", "manifest"}

    repeated = publish_knowledge_source_bundle(bundle, bucket="knowledge", object_store=store)
    assert repeated == result
    assert len(store.uploaded) == 5


def test_publish_refuses_to_overwrite_different_immutable_object(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    publish_knowledge_source_bundle(bundle, bucket="knowledge", object_store=store)
    graph_key = next(key for key in store.uploaded if key.endswith("/graph.json"))
    (store.root / "knowledge" / graph_key).write_text("different", encoding="utf-8")

    with pytest.raises(KnowledgeSourcePublicationError, match="differs"):
        publish_knowledge_source_bundle(bundle, bucket="knowledge", object_store=store)
