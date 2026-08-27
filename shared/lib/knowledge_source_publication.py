"""Immutable Knowledge Source publication and database promotion."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.knowledge_source_graphify import (
    KnowledgeSourceBundleResult,
    verify_knowledge_source_bundle,
)
from shared.lib.models import BackgroundJobRun, KnowledgeSource, KnowledgeSourceVersion
from shared.lib.object_store import ObjectStore, get_object_store

KNOWLEDGE_SOURCE_OBJECT_PREFIX = "knowledge-sources"
_ARTIFACT_ORDER = ("graph", "source", "report", "extraction_log", "manifest")


class KnowledgeSourcePublicationError(RuntimeError):
    """Raised when an immutable object cannot be safely published or promoted."""


@dataclass(frozen=True)
class KnowledgeSourcePublication:
    bucket: str
    artifact_keys: dict[str, str]
    artifact_checksums: dict[str, str]


def publish_knowledge_source_bundle(
    bundle: KnowledgeSourceBundleResult,
    *,
    bucket: str,
    object_store: ObjectStore | None = None,
) -> KnowledgeSourcePublication:
    """Upload every artifact without overwriting a different immutable object."""
    bucket = bucket.strip()
    if not bucket:
        raise KnowledgeSourcePublicationError("Knowledge Source object-store bucket is required")
    manifest = verify_knowledge_source_bundle(bundle.bundle_dir)
    source = manifest["source"]
    source_id = str(source["id"])
    commit_sha = str(source["commit_sha"])
    extraction_config_hash = str(manifest["graphify"]["extraction_config_hash"])
    prefix = f"{KNOWLEDGE_SOURCE_OBJECT_PREFIX}/{source_id}/{commit_sha}/{extraction_config_hash}"
    store = object_store or get_object_store()
    artifact_keys: dict[str, str] = {}
    artifact_checksums: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="knowledge-source-publication-") as temporary_dir:
        verification_root = Path(temporary_dir)
        for name in _ARTIFACT_ORDER:
            local_path = bundle.artifact_paths[name]
            key = f"{prefix}/{local_path.name}"
            expected_size = local_path.stat().st_size
            expected_checksum = _sha256(local_path)
            existing_path = verification_root / f"existing-{local_path.name}"
            if store.download_file(bucket, key, str(existing_path)):
                _require_matching_object(
                    existing_path,
                    key=key,
                    expected_size=expected_size,
                    expected_checksum=expected_checksum,
                )
            else:
                store.upload_file(bucket, key, str(local_path))

            verified_path = verification_root / f"verified-{local_path.name}"
            if not store.download_file(bucket, key, str(verified_path)):
                raise KnowledgeSourcePublicationError(f"Published Knowledge Source artifact is missing: {key}")
            _require_matching_object(
                verified_path,
                key=key,
                expected_size=expected_size,
                expected_checksum=expected_checksum,
            )
            artifact_keys[name] = key
            artifact_checksums[name] = f"sha256:{expected_checksum}"

    return KnowledgeSourcePublication(
        bucket=bucket,
        artifact_keys=artifact_keys,
        artifact_checksums=artifact_checksums,
    )


async def promote_knowledge_source_version(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    publication: KnowledgeSourcePublication,
    file_count: int,
    node_count: int,
    edge_count: int,
    phase_timings: dict[str, float],
    warnings: list[str] | None = None,
    finished_at: datetime | None = None,
) -> KnowledgeSourceVersion:
    """Atomically mark the complete version successful and advance its source."""
    source, version, run = await _locked_sync_records(session, source_id, version_id, run_id)
    if version.status not in {"pending", "running"}:
        raise KnowledgeSourcePublicationError(f"Knowledge Source version is already {version.status}")
    if run.status != "running":
        raise KnowledgeSourcePublicationError(f"Knowledge Source sync run is already {run.status}")

    completed_at = finished_at or datetime.now(UTC)
    safe_warnings = warnings or []
    version.status = "succeeded"
    version.artifact_keys = publication.artifact_keys
    version.artifact_checksums = publication.artifact_checksums
    version.file_count = file_count
    version.node_count = node_count
    version.edge_count = edge_count
    version.warnings = safe_warnings
    version.error = None
    version.finished_at = completed_at
    source.current_successful_version_id = version.id

    run.status = "succeeded"
    run.heartbeat_at = completed_at
    run.finished_at = completed_at
    run.duration_sec = max(0.0, (completed_at - run.started_at).total_seconds())
    run.summary = {
        "commit_sha": version.commit_sha,
        "phase_timings_sec": phase_timings,
        "file_count": file_count,
        "node_count": node_count,
        "edge_count": edge_count,
        "artifact_keys": publication.artifact_keys,
    }
    run.warnings = safe_warnings
    run.error = None
    await session.commit()
    await session.refresh(version)
    return version


async def fail_knowledge_source_version(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    failure_phase: str,
    error: str,
    phase_timings: dict[str, float],
    warnings: list[str] | None = None,
    finished_at: datetime | None = None,
) -> KnowledgeSourceVersion:
    """Atomically fail an attempted version without changing the serving pointer."""
    _source, version, run = await _locked_sync_records(session, source_id, version_id, run_id)
    if run.status != "running":
        raise KnowledgeSourcePublicationError(f"Knowledge Source sync run is already {run.status}")

    completed_at = finished_at or datetime.now(UTC)
    safe_warnings = warnings or []
    version.status = "failed"
    version.warnings = safe_warnings
    version.error = error
    version.finished_at = completed_at
    run.status = "failed"
    run.heartbeat_at = completed_at
    run.finished_at = completed_at
    run.duration_sec = max(0.0, (completed_at - run.started_at).total_seconds())
    run.summary = {
        **(run.summary or {}),
        "commit_sha": version.commit_sha,
        "failure_phase": failure_phase,
        "phase_timings_sec": phase_timings,
    }
    run.warnings = safe_warnings
    run.error = error
    await session.commit()
    await session.refresh(version)
    return version


async def _locked_sync_records(
    session: AsyncSession,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[KnowledgeSource, KnowledgeSourceVersion, BackgroundJobRun]:
    source = await session.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
    version = await session.scalar(
        select(KnowledgeSourceVersion).where(KnowledgeSourceVersion.id == version_id).with_for_update()
    )
    run = await session.scalar(select(BackgroundJobRun).where(BackgroundJobRun.id == run_id).with_for_update())
    if source is None or version is None or run is None:
        raise KnowledgeSourcePublicationError("Knowledge Source sync records are incomplete")
    if version.source_id != source.id:
        raise KnowledgeSourcePublicationError("Knowledge Source version belongs to another source")
    if run.knowledge_source_id != source.id or run.knowledge_source_version_id != version.id:
        raise KnowledgeSourcePublicationError("Background job does not own the Knowledge Source version")
    return source, version, run


def _require_matching_object(
    path: Path,
    *,
    key: str,
    expected_size: int,
    expected_checksum: str,
) -> None:
    if path.stat().st_size != expected_size or _sha256(path) != expected_checksum:
        raise KnowledgeSourcePublicationError(f"Immutable Knowledge Source object differs at {key}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
