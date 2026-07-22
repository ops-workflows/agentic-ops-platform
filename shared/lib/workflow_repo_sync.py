"""Workflow-repo sync pipeline: fetch source, discover workflows, build/upload
bundles, and persist the result as the single ``workflow_repo_state`` row.

This is the one pipeline used both by the initial bootstrap sync and by the
operator-triggered "Sync now" action — "sync" always means: fetch the
(possibly pinned) ref, rediscover workflow packages, rebuild every bundle,
and (if object storage is configured) re-upload them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.config import settings
from shared.lib.models import WorkflowRepoState
from shared.lib.object_store import upload_bytes
from shared.lib.workflow_bundles import (
    WorkflowRepoMetadata,
    build_workflow_bundle,
    git_commit_for_path,
    platform_version_for_root,
    upload_bundle_archive,
)
from shared.lib.workflow_paths import configured_workflow_roots, discover_workflow_packages, sync_workflow_repo_to_ref

logger = logging.getLogger(__name__)

WORKFLOW_REPO_STATE_ID = 1
RELEASES_PREFIX = "releases"

# Compatibility policy: a bundle built with a platform major version newer
# than the running platform cannot be assumed to work (the running platform
# predates that bundle's contract changes). An older bundle major version is
# only a warning: newer platforms are expected to stay backward compatible
# within a major version.
COMPATIBILITY_ERROR = "incompatible"
COMPATIBILITY_WARNING = "warning"
COMPATIBILITY_OK = "ok"


@dataclass
class WorkflowSyncResult:
    status: str  # "ok" | "partial_error" | "error"
    synced_ref: str | None = None
    commit: str | None = None
    discovered_workflows: list[str] = field(default_factory=list)
    bundle_errors: dict[str, str] = field(default_factory=dict)
    compatibility_warnings: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def _major_version(value: str) -> int | None:
    first_segment = value.strip().split(".")[0]
    return int(first_segment) if first_segment.isdigit() else None


def check_bundle_compatibility(bundle_platform_version: str, running_platform_version: str) -> str:
    """Return "ok", "warning", or "incompatible" per the major-version policy."""
    bundle_major = _major_version(bundle_platform_version)
    running_major = _major_version(running_platform_version)
    if bundle_major is None or running_major is None:
        return COMPATIBILITY_OK
    if bundle_major > running_major:
        return COMPATIBILITY_ERROR
    if bundle_major < running_major:
        return COMPATIBILITY_WARNING
    return COMPATIBILITY_OK


async def _load_state(session: AsyncSession) -> WorkflowRepoState:
    state = await session.get(WorkflowRepoState, WORKFLOW_REPO_STATE_ID)
    if state is None:
        state = WorkflowRepoState(id=WORKFLOW_REPO_STATE_ID)
        session.add(state)
        await session.flush()
    return state


async def get_workflow_repo_state(session: AsyncSession) -> WorkflowRepoState | None:
    result = await session.execute(select(WorkflowRepoState).where(WorkflowRepoState.id == WORKFLOW_REPO_STATE_ID))
    return result.scalar_one_or_none()


async def pin_workflow_repo_ref(session: AsyncSession, ref: str) -> WorkflowRepoState:
    state = await _load_state(session)
    state.pinned_ref = ref.strip() or None
    await session.commit()
    await session.refresh(state)
    return state


def _effective_ref(pinned_ref: str | None) -> str | None:
    ref = (pinned_ref or "").strip() or settings.workflow_repo_ref.strip()
    return ref or None


def _platform_config_source_path() -> Path:
    """Return the repo-owned platform config selected by the current sync mode."""
    if settings.workflow_repo_url.strip() and settings.workflow_repo_source.strip().lower() != "local":
        return Path(settings.workflow_repo_local_path).expanduser() / "platform-config.yaml"
    return Path(settings.platform_config_file or settings.platform_secrets_file).expanduser()


def _platform_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _publish_platform_config_snapshot(bundle_root: Path) -> Path:
    """Atomically publish the synced repo config used for new task launches."""
    source = _platform_config_source_path()
    if not source.is_file():
        raise FileNotFoundError(f"Workflow repo platform config was not found: {source}")

    bundle_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".platform-config-", dir=bundle_root, delete=False) as temp_file:
        temporary_path = Path(temp_file.name)
    try:
        shutil.copy2(source, temporary_path)
        temporary_path.replace(bundle_root / "platform-config.yaml")
    finally:
        temporary_path.unlink(missing_ok=True)

    return bundle_root / "platform-config.yaml"


def _release_id(*, commit: str, effective_ref: str | None) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if commit:
        return f"{commit[:12]}-{timestamp}"
    if effective_ref:
        return f"{effective_ref.replace('/', '-')}-{timestamp}"
    return timestamp


def _publish_object_store_release(
    *,
    bucket: str,
    release_id: str,
    platform_config: Path,
    bundles: dict[str, dict[str, str]],
    commit: str,
    effective_ref: str | None,
) -> None:
    """Publish a complete release before advancing the active release pointer."""
    release_prefix = f"{RELEASES_PREFIX}/{release_id}"
    config_key = f"{release_prefix}/platform-config.yaml"
    manifest_key = f"{release_prefix}/manifest.json"
    manifest = {
        "release_id": release_id,
        "commit": commit or None,
        "ref": effective_ref,
        "platform_config_key": config_key,
        "bundles": bundles,
    }
    upload_bytes(bucket, config_key, platform_config.read_bytes(), content_type="application/x-yaml")
    upload_bytes(
        bucket,
        manifest_key,
        json.dumps(manifest, sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )
    upload_bytes(
        bucket,
        f"{RELEASES_PREFIX}/active.json",
        json.dumps({"manifest_key": manifest_key}, sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )


async def sync_workflow_repo(session: AsyncSession) -> WorkflowSyncResult:
    """Run the full sync pipeline and persist the outcome to workflow_repo_state."""
    state = await _load_state(session)
    effective_ref = _effective_ref(state.pinned_ref)

    try:
        result = await _run_sync_pipeline(effective_ref)
    except Exception as exc:  # noqa: BLE001 - sync failures must be recorded, never raised to the caller
        logger.exception("Workflow repo sync failed")
        result = WorkflowSyncResult(status="error", synced_ref=effective_ref, error=str(exc))

    state.last_synced_ref = result.synced_ref
    state.last_synced_commit = result.commit
    state.last_synced_at = datetime.now(UTC)
    state.last_sync_status = result.status
    state.last_sync_error = result.error
    state.discovered_workflows = result.discovered_workflows
    state.bundle_errors = result.bundle_errors
    await session.commit()

    return result


async def _run_sync_pipeline(effective_ref: str | None) -> WorkflowSyncResult:
    import asyncio

    def _sync_blocking() -> WorkflowSyncResult:
        if settings.workflow_repo_url.strip() and settings.workflow_repo_source.strip().lower() != "local":
            sync_workflow_repo_to_ref(effective_ref)  # raises RuntimeError on failure

        packages = discover_workflow_packages()
        discovered = sorted(pkg.name for pkg in packages)

        platform_root = _platform_root()
        running_version = platform_version_for_root(platform_root)
        commit = git_commit_for_path(Path(settings.workflow_repo_local_path).expanduser())

        bundle_errors: dict[str, str] = {}
        compatibility_warnings: dict[str, str] = {}
        bundle_root = settings.runtime_bundle_root.strip()
        bucket = settings.runtime_bundle_object_store_bucket.strip()
        if bundle_root or bucket:
            output_dir = (
                Path(bundle_root).expanduser() if bundle_root else Path(tempfile.mkdtemp(prefix="workflow-release-"))
            )
            release_id = _release_id(commit=commit, effective_ref=effective_ref)
            release_bundles: dict[str, dict[str, str]] = {}
            for package in packages:
                try:
                    build_result = build_workflow_bundle(
                        workflow=package.name,
                        output_dir=output_dir,
                        platform_root=platform_root,
                        workflow_roots=configured_workflow_roots(),
                        repo_metadata=WorkflowRepoMetadata(
                            name=settings.workflow_repo_url or "local",
                            url=settings.workflow_repo_url,
                            ref=effective_ref or "",
                            commit=commit,
                        ),
                    )
                    compatibility = check_bundle_compatibility(
                        build_result.manifest.get("platform_version", ""), running_version
                    )
                    if compatibility == COMPATIBILITY_ERROR:
                        bundle_errors[package.name] = (
                            f"Bundle platform_version {build_result.manifest.get('platform_version')!r} is newer "
                            f"than running platform {running_version!r}"
                        )
                        continue
                    if compatibility == COMPATIBILITY_WARNING:
                        compatibility_warnings[package.name] = (
                            f"Bundle platform_version {build_result.manifest.get('platform_version')!r} is older "
                            f"than running platform {running_version!r}"
                        )
                    if bucket:
                        key = upload_bundle_archive(
                            build_result.bundle_dir,
                            package.name,
                            bucket=bucket,
                            key_prefix=f"{RELEASES_PREFIX}/{release_id}/bundles",
                        )
                        release_bundles[package.name] = {
                            "key": key,
                            "checksum": "sha256:" + hashlib.sha256(build_result.manifest_path.read_bytes()).hexdigest(),
                        }
                except Exception as exc:  # noqa: BLE001 - one workflow's failure must not abort the whole sync
                    logger.exception("Failed to build/upload bundle for workflow %s", package.name)
                    bundle_errors[package.name] = str(exc)

            if not bundle_errors:
                try:
                    snapshot = _publish_platform_config_snapshot(output_dir)
                    if bucket:
                        _publish_object_store_release(
                            bucket=bucket,
                            release_id=release_id,
                            platform_config=snapshot,
                            bundles=release_bundles,
                            commit=commit,
                            effective_ref=effective_ref,
                        )
                except Exception as exc:  # noqa: BLE001 - do not activate an incomplete release snapshot
                    logger.exception("Failed to publish synced platform config")
                    bundle_errors["platform-config"] = str(exc)

            if not bundle_root:
                shutil.rmtree(output_dir, ignore_errors=True)

        status = "ok" if not bundle_errors else "partial_error"
        return WorkflowSyncResult(
            status=status,
            synced_ref=effective_ref,
            commit=commit or None,
            discovered_workflows=discovered,
            bundle_errors=bundle_errors,
            compatibility_warnings=compatibility_warnings,
        )

    return await asyncio.to_thread(_sync_blocking)
