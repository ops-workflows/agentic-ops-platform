"""Controlled local serving cache for immutable Knowledge Source versions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.lib.knowledge_source_graphify import MAX_SOURCE_ARCHIVE_EXPANDED_BYTES
from shared.lib.models import KnowledgeSource, KnowledgeSourceVersion
from shared.lib.object_store import ObjectStore, get_object_store

_SERVING_ARTIFACTS = ("manifest", "graph", "source")


class KnowledgeSourceHydrationError(RuntimeError):
    """Raised when a promoted version cannot be safely hydrated."""


@dataclass(frozen=True)
class HydratedKnowledgeSource:
    source_id: uuid.UUID
    version_id: uuid.UUID
    canonical_alias: str
    version_dir: Path
    graph_path: Path
    source_root: Path


def hydrate_knowledge_source(
    source: KnowledgeSource,
    version: KnowledgeSourceVersion,
    *,
    cache_root: Path,
    bucket: str,
    object_store: ObjectStore | None = None,
    max_expanded_source_bytes: int = MAX_SOURCE_ARCHIVE_EXPANDED_BYTES,
) -> HydratedKnowledgeSource:
    """Hydrate one promoted version and atomically advance its local pointer."""
    bucket = bucket.strip()
    if not bucket:
        raise KnowledgeSourceHydrationError("Knowledge Source object-store bucket is required")
    if version.id != source.current_successful_version_id or version.source_id != source.id:
        raise KnowledgeSourceHydrationError("Knowledge Source version is not the promoted source version")
    if version.status != "succeeded":
        raise KnowledgeSourceHydrationError("Only a successful Knowledge Source version can be hydrated")

    source_root = cache_root.expanduser().resolve() / str(source.id)
    version_dir = source_root / str(version.id)
    store = object_store or get_object_store()
    if not _cached_version_matches(version_dir, source, version):
        temporary_dir = source_root / f".{version.id}.tmp-{uuid.uuid4()}"
        shutil.rmtree(temporary_dir, ignore_errors=True)
        temporary_dir.mkdir(parents=True)
        try:
            _download_serving_artifacts(store, bucket, version, temporary_dir)
            manifest = _validate_downloaded_artifacts(source, version, temporary_dir)
            _extract_source_archive(
                temporary_dir / "source.tar.zst",
                temporary_dir / "source",
                manifest=manifest,
                max_expanded_bytes=max_expanded_source_bytes,
            )
            _validate_serving_graph(temporary_dir / "graph.json", temporary_dir / "source")
            (temporary_dir / ".ready").write_text(_expected_checksum(version, "manifest") + "\n")
            if version_dir.exists():
                shutil.rmtree(version_dir)
            os.replace(temporary_dir, version_dir)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    _publish_current_pointer(source_root, version.id)
    return HydratedKnowledgeSource(
        source_id=source.id,
        version_id=version.id,
        canonical_alias=source.canonical_alias,
        version_dir=version_dir,
        graph_path=version_dir / "graph.json",
        source_root=version_dir / "source",
    )


def _download_serving_artifacts(
    store: ObjectStore,
    bucket: str,
    version: KnowledgeSourceVersion,
    destination: Path,
) -> None:
    file_names = {"manifest": "manifest.json", "graph": "graph.json", "source": "source.tar.zst"}
    for name in _SERVING_ARTIFACTS:
        key = version.artifact_keys.get(name)
        if not isinstance(key, str) or not key or Path(key).name != file_names[name]:
            raise KnowledgeSourceHydrationError(f"Knowledge Source version has no valid {name} artifact key")
        if not store.download_file(bucket, key, str(destination / file_names[name])):
            raise KnowledgeSourceHydrationError(f"Knowledge Source serving artifact is missing: {key}")
        if _sha256(destination / file_names[name]) != _expected_checksum(version, name):
            raise KnowledgeSourceHydrationError(f"Knowledge Source {name} artifact checksum does not match version")


def _validate_downloaded_artifacts(
    source: KnowledgeSource,
    version: KnowledgeSourceVersion,
    version_dir: Path,
) -> dict[str, Any]:
    manifest = _load_json(version_dir / "manifest.json")
    identity = manifest.get("source")
    expected_identity = {
        "id": str(source.id),
        "canonical_alias": source.canonical_alias,
        "repository_url": source.repository_url,
        "commit_sha": version.commit_sha,
        "include_paths": source.include_paths,
        "exclude_paths": source.exclude_paths,
    }
    if identity != expected_identity:
        raise KnowledgeSourceHydrationError("Knowledge Source manifest identity or scope does not match registry")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise KnowledgeSourceHydrationError("Knowledge Source manifest has no artifact metadata")
    for name, file_name in (("graph", "graph.json"), ("source", "source.tar.zst")):
        entry = artifacts.get(name)
        path = version_dir / file_name
        if not isinstance(entry, dict) or entry.get("file") != file_name:
            raise KnowledgeSourceHydrationError(f"Knowledge Source manifest has invalid {name} metadata")
        if entry.get("size_bytes") != path.stat().st_size or entry.get("sha256") != _sha256(path):
            raise KnowledgeSourceHydrationError(f"Knowledge Source manifest does not match {name} artifact")
    return manifest


def _extract_source_archive(
    archive_path: Path,
    destination: Path,
    *,
    manifest: dict[str, Any],
    max_expanded_bytes: int,
) -> None:
    import zstandard

    if max_expanded_bytes <= 0:
        raise ValueError("Expanded source archive limit must be positive")
    source_metadata = manifest["artifacts"]["source"]
    expanded_size = 0
    file_count = 0
    destination.mkdir()
    with (
        archive_path.open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
        tarfile.open(fileobj=reader, mode="r|") as archive,
    ):
        for member in archive:
            relative_path = _safe_relative_path(member.name)
            if not member.isfile():
                raise KnowledgeSourceHydrationError("Knowledge Source archive may contain only regular files")
            expanded_size += member.size
            file_count += 1
            if expanded_size > max_expanded_bytes:
                raise KnowledgeSourceHydrationError("Knowledge Source archive exceeds expanded-size limit")
            source_file = archive.extractfile(member)
            if source_file is None:
                raise KnowledgeSourceHydrationError(f"Could not read Knowledge Source archive member {member.name}")
            target = destination / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(source_file, output)
    if source_metadata.get("expanded_size_bytes") != expanded_size or source_metadata.get("file_count") != file_count:
        raise KnowledgeSourceHydrationError("Knowledge Source archive counts do not match manifest")


def _validate_serving_graph(graph_path: Path, source_root: Path) -> None:
    graph = _load_json(graph_path)
    nodes = graph.get("nodes")
    links = graph.get("links")
    hyperedges = graph.get("hyperedges")
    if not isinstance(nodes, list) or not isinstance(links, list) or not isinstance(hyperedges, list):
        raise KnowledgeSourceHydrationError("Knowledge Source graph has an invalid node-link schema")
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise KnowledgeSourceHydrationError("Knowledge Source graph has an invalid node")
        if node["id"] in node_ids:
            raise KnowledgeSourceHydrationError("Knowledge Source graph has duplicate node IDs")
        node_ids.add(node["id"])
        source_file = node.get("source_file")
        if source_file not in (None, "") and not (source_root / _safe_relative_path(source_file)).is_file():
            raise KnowledgeSourceHydrationError("Knowledge Source graph references a file absent from its snapshot")
    for link in links:
        if not isinstance(link, dict) or link.get("source") not in node_ids or link.get("target") not in node_ids:
            raise KnowledgeSourceHydrationError("Knowledge Source graph has an invalid link")


def _cached_version_matches(
    version_dir: Path,
    source: KnowledgeSource,
    version: KnowledgeSourceVersion,
) -> bool:
    ready_path = version_dir / ".ready"
    required = (version_dir / "manifest.json", version_dir / "graph.json", version_dir / "source.tar.zst")
    if (
        not ready_path.is_file()
        or not all(path.is_file() for path in required)
        or not (version_dir / "source").is_dir()
    ):
        return False
    try:
        if ready_path.read_text(encoding="utf-8").strip() != _expected_checksum(version, "manifest"):
            return False
        for name, file_name in (("manifest", "manifest.json"), ("graph", "graph.json"), ("source", "source.tar.zst")):
            if _sha256(version_dir / file_name) != _expected_checksum(version, name):
                return False
        _validate_downloaded_artifacts(source, version, version_dir)
        _validate_extracted_source_archive(version_dir / "source.tar.zst", version_dir / "source")
        _validate_serving_graph(version_dir / "graph.json", version_dir / "source")
        return True
    except (OSError, UnicodeDecodeError, KnowledgeSourceHydrationError):
        return False


def _validate_extracted_source_archive(archive_path: Path, source_root: Path) -> None:
    import zstandard

    archived_paths: set[Path] = set()
    with (
        archive_path.open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
        tarfile.open(fileobj=reader, mode="r|") as archive,
    ):
        for member in archive:
            relative_path = _safe_relative_path(member.name)
            archived_paths.add(relative_path)
            extracted_path = source_root / relative_path
            archived_file = archive.extractfile(member)
            if not member.isfile() or archived_file is None or not extracted_path.is_file():
                raise KnowledgeSourceHydrationError("Knowledge Source extracted snapshot is incomplete")
            digest = hashlib.sha256()
            while chunk := archived_file.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != _sha256(extracted_path):
                raise KnowledgeSourceHydrationError("Knowledge Source extracted snapshot differs from its archive")
    extracted_paths = {path.relative_to(source_root) for path in source_root.rglob("*") if path.is_file()}
    if extracted_paths != archived_paths:
        raise KnowledgeSourceHydrationError("Knowledge Source extracted snapshot has unexpected files")


def _publish_current_pointer(source_root: Path, version_id: uuid.UUID) -> None:
    source_root.mkdir(parents=True, exist_ok=True)
    temporary_pointer = source_root / f".current-{uuid.uuid4()}"
    temporary_pointer.symlink_to(str(version_id), target_is_directory=True)
    os.replace(temporary_pointer, source_root / "current")


def _expected_checksum(version: KnowledgeSourceVersion, name: str) -> str:
    value = version.artifact_checksums.get(name)
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise KnowledgeSourceHydrationError(f"Knowledge Source version has no valid {name} checksum")
    digest = value.removeprefix("sha256:")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise KnowledgeSourceHydrationError(f"Knowledge Source version has no valid {name} checksum") from exc
    return digest


def _safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise KnowledgeSourceHydrationError("Knowledge Source archive contains an unsafe path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise KnowledgeSourceHydrationError("Knowledge Source archive contains an unsafe path")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeSourceHydrationError(f"Could not read Knowledge Source JSON artifact {path.name}") from exc
    if not isinstance(value, dict):
        raise KnowledgeSourceHydrationError(f"Knowledge Source JSON artifact {path.name} must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
