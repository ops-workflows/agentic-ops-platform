"""Unit tests for memory-sync helpers.

Tests the tarball normalization and volume naming logic. The MinIO
upload/download paths are covered at the service layer with a mock
storage backend.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from session_manager.memory_sync import (
    _extract_archive_to_directory,
    _get_volume_name,
    _memory_filesystem_path,
    _normalize_archive,
    _stream_to_bytes,
    _tar_directory,
    backup_memory,
    restore_memory,
)

pytestmark = pytest.mark.unit


def test_volume_name_convention():
    assert _get_volume_name("platform-test") == "agent-memory-platform-test"
    assert _get_volume_name("incident-investigator") == "agent-memory-incident-investigator"


def test_stream_to_bytes_concatenates_chunks():
    chunks = [b"hello", b" ", b"world"]
    assert _stream_to_bytes(iter(chunks)) == b"hello world"


def _make_tar_archive(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a tar archive matching Docker's get_archive() output shape:
    a top-level directory containing the files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        # Docker's get_archive wraps everything under a top-level dir
        # named after the mount point's last segment.
        root_info = tarfile.TarInfo(name="memory")
        root_info.type = tarfile.DIRTYPE
        tar.addfile(root_info)
        for name, content in entries:
            info = tarfile.TarInfo(name=f"memory/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return buf.read()


def test_normalize_archive_strips_top_level_dir():
    raw = _make_tar_archive(
        [
            ("notes.md", b"hello notes"),
            ("deep/nested.txt", b"nested content"),
        ]
    )
    normalized = _normalize_archive(raw)
    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:") as tar:
        names = sorted(m.name for m in tar.getmembers())
    # Top-level "memory" dir removed; files live at their expected paths
    assert "notes.md" in names
    assert "deep/nested.txt" in names
    # No stray entry named just "memory"
    assert "memory" not in names


def test_normalize_archive_preserves_file_contents():
    raw = _make_tar_archive([("notes.md", b"the body")])
    normalized = _normalize_archive(raw)
    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:") as tar:
        member = tar.getmember("notes.md")
        extracted = tar.extractfile(member).read()
    assert extracted == b"the body"


def test_normalize_archive_empty_input_returns_empty_archive():
    # An empty tar (no entries) should produce an empty-but-valid tar
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:"):
        pass
    normalized = _normalize_archive(buf.getvalue())
    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:") as tar:
        assert tar.getmembers() == []


def test_filesystem_memory_path_supports_agent_placeholder(monkeypatch, tmp_path):
    from shared.lib.config import settings

    monkeypatch.setattr(settings, "memory_filesystem_root", str(tmp_path / "{agent}"))
    assert _memory_filesystem_path("platform-test") == tmp_path / "platform-test"


def test_tar_and_extract_directory_round_trip(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("hello")
    (source / "nested").mkdir()
    (source / "nested" / "x.txt").write_text("x")

    archive = _tar_directory(source)
    target = tmp_path / "target"
    _extract_archive_to_directory(archive, target)

    assert (target / "notes.md").read_text() == "hello"
    assert (target / "nested" / "x.txt").read_text() == "x"


def test_extract_directory_rejects_path_traversal(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"bad"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))

    with pytest.raises(ValueError, match="Unsafe path"):
        _extract_archive_to_directory(buffer.getvalue(), tmp_path / "target")


@pytest.mark.asyncio
async def test_filesystem_backup_and_restore_uses_object_storage(monkeypatch, tmp_path):
    from shared.lib.config import settings

    object_store: dict[str, bytes] = {}

    def fake_upload(bucket, key, data, *, content_type="application/octet-stream"):
        object_store[f"{bucket}/{key}"] = data
        return key

    def fake_download(bucket, key):
        return object_store.get(f"{bucket}/{key}")

    monkeypatch.setattr(settings, "memory_sync_mode", "filesystem")
    monkeypatch.setattr(settings, "memory_filesystem_root", str(tmp_path / "memory"))
    monkeypatch.setattr("session_manager.memory_sync.upload_bytes", fake_upload)
    monkeypatch.setattr("session_manager.memory_sync.download_bytes", fake_download)

    memory_dir = tmp_path / "memory" / "platform-test"
    memory_dir.mkdir(parents=True)
    (memory_dir / "notes.md").write_text("persist me")

    assert await backup_memory("platform-test") is True
    assert "agent-memory/platform-test/latest.tar.gz" in object_store

    for path in sorted(memory_dir.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    assert await restore_memory("platform-test") is True
    assert (memory_dir / "notes.md").read_text() == "persist me"
