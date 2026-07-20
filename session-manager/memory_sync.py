"""Memory Sync — MinIO backup/restore for per-agent memory volumes.

Docker volumes are a local cache — MinIO is the durable source of truth.
Session Manager backs up memory to MinIO after every session completion
and restores from MinIO when a volume is empty (e.g., after host restart).

Backup: after session → tar volume → upload to MinIO (latest + versioned)
Restore: before session → if volume empty → download from MinIO → extract
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import docker

from shared.lib.config import settings
from shared.lib.object_store import (
    BUCKET_AGENT_MEMORY,
    download_bytes,
    download_file,
    upload_bytes,
    upload_file,
)

logger = logging.getLogger(__name__)

MEMORY_HELPER_IMAGE = os.environ.get("MEMORY_HELPER_IMAGE", "alpine:3.20")
MEMORY_HELPER_PATH = "/memory"
MEMORY_COMPLETE_MARKER = ".agentic-ops-memory-complete"
_docker_client: docker.DockerClient | None = None


def _get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def _get_volume_name(agent_name: str) -> str:
    return f"agent-memory-{agent_name}"


def _create_memory_helper(volume_name: str, *, mode: str):
    client = _get_docker_client()
    helper_name = f"memory-helper-{volume_name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    container = client.containers.create(
        image=MEMORY_HELPER_IMAGE,
        command=["sh", "-lc", "sleep 600"],
        name=helper_name,
        detach=True,
        volumes={volume_name: {"bind": MEMORY_HELPER_PATH, "mode": mode}},
        labels={"ai-ops.memory-helper": "true"},
    )
    container.start()
    return container


def _helper_has_memory_files(container) -> bool:
    result = container.exec_run(["sh", "-lc", f"find {MEMORY_HELPER_PATH} -mindepth 1 -print -quit"])
    output = (
        result.output.decode("utf-8", errors="ignore")
        if isinstance(result.output, (bytes, bytearray))
        else str(result.output)
    )
    return result.exit_code == 0 and bool(output.strip())


def _stream_to_bytes(stream) -> bytes:
    return b"".join(chunk for chunk in stream)


def _normalize_archive(raw_archive: bytes) -> bytes:
    source_buffer = io.BytesIO(raw_archive)
    normalized_buffer = io.BytesIO()

    with (
        tarfile.open(fileobj=source_buffer, mode="r:") as source_tar,
        tarfile.open(fileobj=normalized_buffer, mode="w:") as normalized_tar,
    ):
        for member in source_tar.getmembers():
            parts = [part for part in member.name.split("/") if part not in {"", "."}]
            if len(parts) <= 1:
                continue

            member.name = "/".join(parts[1:])
            file_obj = source_tar.extractfile(member) if member.isfile() else None
            normalized_tar.addfile(member, fileobj=file_obj)
            if file_obj is not None:
                file_obj.close()

    normalized_buffer.seek(0)
    return normalized_buffer.read()


def _memory_filesystem_path(agent_name: str) -> Path:
    root = settings.memory_filesystem_root.replace("{agent}", agent_name)
    path = Path(root)
    if "{agent}" in settings.memory_filesystem_root:
        return path
    return path / agent_name


def _directory_has_files(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _tar_directory(path: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for item in sorted(path.rglob("*")):
            archive.add(item, arcname=str(item.relative_to(path)))
    buffer.seek(0)
    return buffer.read()


def _extract_archive_to_directory(archive_bytes: bytes, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    target_root = path.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            member_path = (path / member.name).resolve()
            if not str(member_path).startswith(f"{target_root}{os.sep}") and member_path != target_root:
                raise ValueError(f"Unsafe path in memory archive: {member.name}")
            if member.isdir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, member_path.open("wb") as target:
                shutil.copyfileobj(source, target)


async def backup_memory(agent_name: str) -> bool:
    if settings.memory_sync_mode.strip().lower() == "filesystem":
        return await _backup_filesystem_memory(agent_name)
    return await _backup_docker_volume_memory(agent_name)


async def _backup_docker_volume_memory(agent_name: str) -> bool:
    """Backup agent memory volume to MinIO after session completion.

    Creates both a 'latest' and a timestamped version for history.
    """
    volume_name = _get_volume_name(agent_name)
    helper = None
    tar_path: str | None = None

    try:
        helper = _create_memory_helper(volume_name, mode="ro")
        if not _helper_has_memory_files(helper):
            logger.info("No memory to backup for agent '%s'", agent_name)
            return False

        raw_archive_stream, _ = helper.get_archive(MEMORY_HELPER_PATH)
        normalized_archive = _normalize_archive(_stream_to_bytes(raw_archive_stream))
        if not normalized_archive:
            logger.info("No memory to backup for agent '%s'", agent_name)
            return False

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tar_path = tmp.name

        with (
            tarfile.open(tar_path, mode="w:gz") as archive,
            tarfile.open(fileobj=io.BytesIO(normalized_archive), mode="r:") as normalized_tar,
        ):
            for member in normalized_tar.getmembers():
                file_obj = normalized_tar.extractfile(member) if member.isfile() else None
                archive.addfile(member, fileobj=file_obj)
                if file_obj is not None:
                    file_obj.close()

        # Upload as latest
        upload_file(BUCKET_AGENT_MEMORY, f"{agent_name}/latest.tar.gz", tar_path)

        # Upload timestamped version
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        upload_file(BUCKET_AGENT_MEMORY, f"{agent_name}/{timestamp}.tar.gz", tar_path)

        file_size = os.path.getsize(tar_path)
        logger.info("Backed up memory for '%s' to MinIO (%d bytes)", agent_name, file_size)
        return True

    except Exception:
        logger.exception("Failed to backup memory for '%s'", agent_name)
        return False
    finally:
        if helper is not None:
            try:
                helper.remove(force=True)
            except Exception:
                logger.warning("Failed to remove memory helper for '%s'", agent_name)
        # Cleanup temp file
        if tar_path and os.path.exists(tar_path):
            os.unlink(tar_path)


async def restore_memory(agent_name: str) -> bool:
    if settings.memory_sync_mode.strip().lower() == "filesystem":
        return await _restore_filesystem_memory(agent_name)
    return await _restore_docker_volume_memory(agent_name)


async def _restore_docker_volume_memory(agent_name: str) -> bool:
    """Restore agent memory from MinIO if Docker volume is empty.

    Called before spawning a new session. If the volume already has data,
    this is a no-op (data from the previous session is still there).
    """
    volume_name = _get_volume_name(agent_name)
    helper = None
    restore_path: str | None = None

    try:
        helper = _create_memory_helper(volume_name, mode="rw")
        if _helper_has_memory_files(helper):
            logger.debug("Memory volume for '%s' already has data, skipping restore", agent_name)
            return False

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            restore_path = tmp.name

        # Download latest backup from MinIO
        found = download_file(BUCKET_AGENT_MEMORY, f"{agent_name}/latest.tar.gz", restore_path)
        if not found:
            logger.info("No backup found for '%s' — first session", agent_name)
            return False

        with tarfile.open(restore_path, mode="r:gz") as compressed_tar:
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w:") as plain_tar:
                for member in compressed_tar.getmembers():
                    file_obj = compressed_tar.extractfile(member) if member.isfile() else None
                    plain_tar.addfile(member, fileobj=file_obj)
                    if file_obj is not None:
                        file_obj.close()

        tar_buffer.seek(0)
        if not helper.put_archive(MEMORY_HELPER_PATH, tar_buffer.read()):
            logger.error("Failed to extract memory archive into volume for '%s'", agent_name)
            return False

        logger.info("Restored memory for '%s' from MinIO", agent_name)
        return True

    except Exception:
        logger.exception("Failed to restore memory for '%s'", agent_name)
        return False
    finally:
        if helper is not None:
            try:
                helper.remove(force=True)
            except Exception:
                logger.warning("Failed to remove restore helper for '%s'", agent_name)
        if restore_path and os.path.exists(restore_path):
            os.unlink(restore_path)


async def _backup_filesystem_memory(agent_name: str) -> bool:
    memory_path = _memory_filesystem_path(agent_name)
    try:
        if not _directory_has_files(memory_path):
            logger.info("No filesystem memory to backup for agent '%s'", agent_name)
            return False

        archive_bytes = _tar_directory(memory_path)
        if not archive_bytes:
            return False

        upload_bytes(
            BUCKET_AGENT_MEMORY,
            f"{agent_name}/latest.tar.gz",
            archive_bytes,
            content_type="application/gzip",
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        upload_bytes(
            BUCKET_AGENT_MEMORY,
            f"{agent_name}/{timestamp}.tar.gz",
            archive_bytes,
            content_type="application/gzip",
        )
        logger.info("Backed up filesystem memory for '%s' from %s", agent_name, memory_path)
        return True
    except Exception:
        logger.exception("Failed to backup filesystem memory for '%s'", agent_name)
        return False


async def _restore_filesystem_memory(agent_name: str) -> bool:
    memory_path = _memory_filesystem_path(agent_name)
    try:
        if _directory_has_files(memory_path):
            logger.debug("Filesystem memory for '%s' already has data, skipping restore", agent_name)
            return False

        archive_bytes = download_bytes(BUCKET_AGENT_MEMORY, f"{agent_name}/latest.tar.gz")
        if archive_bytes is None:
            logger.info("No backup found for '%s' — first session", agent_name)
            return False

        _extract_archive_to_directory(archive_bytes, memory_path)
        logger.info("Restored filesystem memory for '%s' into %s", agent_name, memory_path)
        return True
    except Exception:
        logger.exception("Failed to restore filesystem memory for '%s'", agent_name)
        return False


async def restore_memory_to_path(agent_name: str, path: Path) -> bool:
    """Restore one agent archive into a Kubernetes Job's task-local volume."""
    archive_bytes = download_bytes(BUCKET_AGENT_MEMORY, f"{agent_name}/latest.tar.gz")
    if archive_bytes is None:
        logger.info("No backup found for '%s' — first session", agent_name)
        return False
    _extract_archive_to_directory(archive_bytes, path)
    logger.info("Restored memory for '%s' into %s", agent_name, path)
    return True


async def backup_memory_from_path(agent_name: str, path: Path) -> bool:
    """Persist one Kubernetes Job's task-local memory volume."""
    if not _directory_has_files(path):
        logger.info("No memory to backup for '%s'", agent_name)
        return False
    archive_bytes = _tar_directory(path)
    upload_bytes(BUCKET_AGENT_MEMORY, f"{agent_name}/latest.tar.gz", archive_bytes, content_type="application/gzip")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    upload_bytes(
        BUCKET_AGENT_MEMORY,
        f"{agent_name}/{timestamp}.tar.gz",
        archive_bytes,
        content_type="application/gzip",
    )
    logger.info("Backed up memory for '%s' from %s", agent_name, path)
    return True


async def _memory_helper_main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in {"restore", "wait-upload"}:
        print(
            "usage: python -m session_manager.memory_sync {restore|wait-upload} AGENT_NAME MEMORY_PATH",
            file=sys.stderr,
        )
        return 2
    action, agent_name, raw_path = argv[1:]
    path = Path(raw_path)
    if action == "restore":
        await restore_memory_to_path(agent_name, path)
        return 0
    marker = path / MEMORY_COMPLETE_MARKER
    while not marker.exists():
        await asyncio.sleep(1)
    marker.unlink(missing_ok=True)
    await backup_memory_from_path(agent_name, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_memory_helper_main(sys.argv)))
