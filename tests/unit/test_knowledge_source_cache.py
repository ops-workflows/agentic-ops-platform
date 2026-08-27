"""Knowledge Source serving-cache hydration tests."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.lib.knowledge_source_cache import KnowledgeSourceHydrationError, hydrate_knowledge_source
from tests.unit.test_knowledge_source_publication import LocalObjectStore, _bundle

pytestmark = pytest.mark.unit


def _published_version(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bundle = _bundle(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    source_id = uuid.UUID(bundle.manifest["source"]["id"])
    version_id = uuid.uuid4()
    prefix = (
        f"knowledge-sources/{source_id}/{bundle.manifest['source']['commit_sha']}"
        f"/{bundle.manifest['graphify']['extraction_config_hash']}"
    )
    artifact_keys = {}
    artifact_checksums = {}
    for name, path in bundle.artifact_paths.items():
        key = f"{prefix}/{path.name}"
        destination = store.root / "knowledge" / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        artifact_keys[name] = key
        artifact_checksums[name] = bundle.artifact_checksums[name]
    source = SimpleNamespace(
        id=source_id,
        canonical_alias="org/example",
        repository_url="https://github.example.com/org/example.git",
        include_paths=[],
        exclude_paths=[],
        current_successful_version_id=version_id,
    )
    version = SimpleNamespace(
        id=version_id,
        source_id=source_id,
        commit_sha="a" * 40,
        status="succeeded",
        artifact_keys=artifact_keys,
        artifact_checksums=artifact_checksums,
    )
    return source, version, store


def test_hydration_verifies_bundle_and_atomically_publishes_current(tmp_path: Path) -> None:
    source, version, store = _published_version(tmp_path)
    cache_root = tmp_path / "cache"

    hydrated = hydrate_knowledge_source(
        source,
        version,
        cache_root=cache_root,
        bucket="knowledge",
        object_store=store,
    )

    assert hydrated.graph_path.is_file()
    assert (hydrated.source_root / "app.py").read_text(encoding="utf-8").startswith("def main")
    assert (cache_root / str(source.id) / "current").resolve() == hydrated.version_dir


def test_failed_refresh_preserves_previous_current_pointer(tmp_path: Path) -> None:
    source, first_version, store = _published_version(tmp_path / "first")
    cache_root = tmp_path / "cache"
    first = hydrate_knowledge_source(
        source,
        first_version,
        cache_root=cache_root,
        bucket="knowledge",
        object_store=store,
    )
    previous_pointer = (cache_root / str(source.id) / "current").resolve()

    source.current_successful_version_id = uuid.uuid4()
    broken_version = SimpleNamespace(
        id=source.current_successful_version_id,
        source_id=source.id,
        commit_sha="b" * 40,
        status="succeeded",
        artifact_keys=first_version.artifact_keys,
        artifact_checksums={**first_version.artifact_checksums, "graph": "sha256:" + "0" * 64},
    )
    with pytest.raises(KnowledgeSourceHydrationError, match="checksum"):
        hydrate_knowledge_source(
            source,
            broken_version,
            cache_root=cache_root,
            bucket="knowledge",
            object_store=store,
        )

    assert (cache_root / str(source.id) / "current").resolve() == previous_pointer == first.version_dir


def test_persistent_cache_reuse_rehydrates_locally_tampered_artifacts(tmp_path: Path) -> None:
    source, version, store = _published_version(tmp_path)
    cache_root = tmp_path / "cache"
    hydrated = hydrate_knowledge_source(
        source,
        version,
        cache_root=cache_root,
        bucket="knowledge",
        object_store=store,
    )
    hydrated.graph_path.write_text("{}", encoding="utf-8")
    (hydrated.source_root / "app.py").write_text("tampered\n", encoding="utf-8")

    reused = hydrate_knowledge_source(
        source,
        version,
        cache_root=cache_root,
        bucket="knowledge",
        object_store=store,
    )

    assert '"nodes"' in reused.graph_path.read_text(encoding="utf-8")
    assert (reused.source_root / "app.py").read_text(encoding="utf-8").startswith("def main")
