"""Pinned Graphify execution and output validation for Knowledge Sources."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAPHIFY_PACKAGE = "graphifyy"
GRAPHIFY_VERSION = "0.9.40"
GRAPHIFY_GRAPH_FILE = "graph.json"
GRAPHIFY_MANIFEST_FILE = "manifest.json"
GRAPHIFY_REPORT_FILE = "GRAPH_REPORT.md"
KNOWLEDGE_BUNDLE_SCHEMA_VERSION = 1
MAX_SOURCE_ARCHIVE_EXPANDED_BYTES = 1024 * 1024 * 1024


class GraphifyOutputError(ValueError):
    """Raised when Graphify output does not satisfy the serving contract."""


class GraphifyExecutionError(RuntimeError):
    """Raised when the pinned Graphify process cannot complete extraction."""


class KnowledgeSourceBundleError(ValueError):
    """Raised when an immutable Knowledge Source bundle is incomplete or unsafe."""


@dataclass(frozen=True)
class GraphifyExtractionResult:
    output_dir: Path
    graph_path: Path
    manifest_path: Path
    report_path: Path
    file_count: int
    node_count: int
    edge_count: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class KnowledgeSourceBundleResult:
    bundle_dir: Path
    artifact_paths: dict[str, Path]
    artifact_checksums: dict[str, str]
    manifest: dict[str, Any]
    file_count: int
    node_count: int
    edge_count: int


def run_graphify_code_extraction(
    source_dir: Path,
    output_root: Path,
    *,
    graphify_binary: str = "graphify",
    timeout_sec: int = 1800,
) -> GraphifyExtractionResult:
    """Extract or incrementally refresh one code graph using one CLI contract."""
    source_dir = source_dir.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not source_dir.is_dir():
        raise GraphifyExecutionError(f"Knowledge Source directory does not exist: {source_dir}")
    if timeout_sec <= 0:
        raise ValueError("Graphify timeout must be positive")

    commands = [
        [
            graphify_binary,
            "extract",
            str(source_dir),
            "--code-only",
            "--max-workers",
            "1",
            "--no-cluster",
            "--out",
            str(output_root),
        ],
        [
            graphify_binary,
            "cluster-only",
            str(output_root),
            "--no-viz",
        ],
    ]
    completed_steps = []
    for command in commands:
        try:
            completed_steps.append(
                subprocess.run(  # noqa: S603 - executable path is indexer-owned configuration.
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
            )
        except subprocess.TimeoutExpired as exc:
            raise GraphifyExecutionError(f"Graphify {command[1]} timed out after {timeout_sec} seconds") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or f"Graphify {command[1]} failed").strip()
            raise GraphifyExecutionError(detail[-4000:]) from exc
        except OSError as exc:
            raise GraphifyExecutionError(f"Could not execute pinned Graphify {GRAPHIFY_VERSION}: {exc}") from exc

    result = validate_graphify_output(output_root / "graphify-out")
    return GraphifyExtractionResult(
        output_dir=result.output_dir,
        graph_path=result.graph_path,
        manifest_path=result.manifest_path,
        report_path=result.report_path,
        file_count=result.file_count,
        node_count=result.node_count,
        edge_count=result.edge_count,
        stdout="\n".join(step.stdout.strip() for step in completed_steps if step.stdout.strip()),
        stderr="\n".join(step.stderr.strip() for step in completed_steps if step.stderr.strip()),
    )


def validate_graphify_output(output_dir: Path) -> GraphifyExtractionResult:
    """Validate Graphify's persisted graph, extraction manifest, and report."""
    output_dir = output_dir.expanduser().resolve()
    graph_path = _required_file(output_dir, GRAPHIFY_GRAPH_FILE)
    manifest_path = _required_file(output_dir, GRAPHIFY_MANIFEST_FILE)
    report_path = _required_file(output_dir, GRAPHIFY_REPORT_FILE)

    graph = _load_json_object(graph_path)
    nodes = _required_list(graph, "nodes", graph_path)
    edges = _required_list(graph, "links", graph_path)
    _required_list(graph, "hyperedges", graph_path)
    manifest = _load_json_object(manifest_path)

    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise GraphifyOutputError(f"{graph_path.name} node {index} must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphifyOutputError(f"{graph_path.name} node {index} has no string id")
        if node_id in node_ids:
            raise GraphifyOutputError(f"{graph_path.name} contains duplicate node id {node_id!r}")
        node_ids.add(node_id)
        source_file = node.get("source_file")
        if source_file not in (None, ""):
            _validate_relative_path(source_file, f"node {node_id!r} source_file")

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise GraphifyOutputError(f"{graph_path.name} edge {index} must be an object")
        source = edge.get("source")
        target = edge.get("target")
        if source not in node_ids or target not in node_ids:
            raise GraphifyOutputError(f"{graph_path.name} edge {index} references an unknown node")

    for source_file, state in manifest.items():
        _validate_relative_path(source_file, f"manifest file {source_file!r}")
        if not isinstance(state, dict):
            raise GraphifyOutputError(f"{manifest_path.name} entry {source_file!r} must be an object")
        if not isinstance(state.get("ast_hash"), str) or not state["ast_hash"]:
            raise GraphifyOutputError(f"{manifest_path.name} entry {source_file!r} has no AST hash")

    return GraphifyExtractionResult(
        output_dir=output_dir,
        graph_path=graph_path,
        manifest_path=manifest_path,
        report_path=report_path,
        file_count=len(manifest),
        node_count=len(nodes),
        edge_count=len(edges),
    )


def build_knowledge_source_bundle(
    *,
    source_id: str,
    canonical_alias: str,
    repository_url: str,
    commit_sha: str,
    source_dir: Path,
    graphify: GraphifyExtractionResult,
    bundle_dir: Path,
    include_paths: list[str],
    exclude_paths: list[str],
    extraction_config_hash: str,
    extraction_stdout: str = "",
    extraction_stderr: str = "",
) -> KnowledgeSourceBundleResult:
    """Build and verify one self-contained immutable serving bundle."""
    try:
        normalized_source_id = str(uuid.UUID(source_id))
    except ValueError as exc:
        raise KnowledgeSourceBundleError("Knowledge Source ID must be a UUID") from exc
    if not _is_hex_digest(commit_sha, 40):
        raise KnowledgeSourceBundleError("Knowledge Source commit must be a 40-character SHA")
    if not _is_hex_digest(extraction_config_hash, 64):
        raise KnowledgeSourceBundleError("Extraction config hash must be a SHA-256 digest")

    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise KnowledgeSourceBundleError(f"Source snapshot directory does not exist: {source_dir}")
    bundle_dir = bundle_dir.expanduser().resolve()
    if bundle_dir.exists():
        raise KnowledgeSourceBundleError(f"Immutable bundle directory already exists: {bundle_dir}")

    selected_files = _scoped_source_files(source_dir, include_paths, exclude_paths)
    selected_paths = {relative_path for relative_path, _ in selected_files}
    graphify_manifest = _load_json_object(graphify.manifest_path)
    missing_indexed_paths = sorted(set(graphify_manifest) - selected_paths)
    if missing_indexed_paths:
        sample = ", ".join(missing_indexed_paths[:3])
        raise KnowledgeSourceBundleError(
            f"Graphify indexed files are absent from the configured source snapshot: {sample}"
        )

    bundle_dir.mkdir(parents=True)
    artifact_paths = {
        "graph": bundle_dir / "graph.json",
        "source": bundle_dir / "source.tar.zst",
        "report": bundle_dir / "graph-report.md",
        "extraction_log": bundle_dir / "extraction.log",
        "manifest": bundle_dir / "manifest.json",
    }
    try:
        graph = _load_json_object(graphify.graph_path)
        _normalize_graph_source_files(graph, selected_paths)
        artifact_paths["graph"].write_text(json.dumps(graph, separators=(",", ":")), encoding="utf-8")
        shutil.copyfile(graphify.report_path, artifact_paths["report"])
        log = extraction_stdout
        if extraction_stderr:
            log = f"{log.rstrip()}\n{extraction_stderr}" if log else extraction_stderr
        artifact_paths["extraction_log"].write_text(log, encoding="utf-8")
        expanded_size = _write_source_archive(artifact_paths["source"], selected_files)

        artifact_entries: dict[str, dict[str, Any]] = {}
        for name in ("graph", "source", "report", "extraction_log"):
            path = artifact_paths[name]
            artifact_entries[name] = {
                "file": path.name,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        artifact_entries["source"]["expanded_size_bytes"] = expanded_size
        artifact_entries["source"]["file_count"] = len(selected_files)

        manifest: dict[str, Any] = {
            "schema_version": KNOWLEDGE_BUNDLE_SCHEMA_VERSION,
            "source": {
                "id": normalized_source_id,
                "canonical_alias": canonical_alias,
                "repository_url": repository_url,
                "commit_sha": commit_sha,
                "include_paths": include_paths,
                "exclude_paths": exclude_paths,
            },
            "graphify": {
                "package": GRAPHIFY_PACKAGE,
                "version": GRAPHIFY_VERSION,
                "command": ["extract", "--code-only"],
                "extraction_config_hash": extraction_config_hash,
            },
            "counts": {
                "source_files": len(selected_files),
                "graph_files": graphify.file_count,
                "nodes": graphify.node_count,
                "edges": graphify.edge_count,
            },
            "artifacts": artifact_entries,
        }
        artifact_paths["manifest"].write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verified_manifest = verify_knowledge_source_bundle(bundle_dir)
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise

    artifact_checksums = {name: f"sha256:{_sha256(path)}" for name, path in artifact_paths.items()}
    return KnowledgeSourceBundleResult(
        bundle_dir=bundle_dir,
        artifact_paths=artifact_paths,
        artifact_checksums=artifact_checksums,
        manifest=verified_manifest,
        file_count=len(selected_files),
        node_count=graphify.node_count,
        edge_count=graphify.edge_count,
    )


def _normalize_graph_source_files(graph: dict[str, Any], selected_paths: set[str]) -> None:
    """Mark Graphify pseudo paths as external when no archived source file exists."""
    for node in graph["nodes"]:
        source_file = node.get("source_file")
        if source_file not in (None, "") and source_file not in selected_paths:
            node["source_file"] = ""


def verify_knowledge_source_bundle(
    bundle_dir: Path,
    *,
    max_expanded_source_bytes: int = MAX_SOURCE_ARCHIVE_EXPANDED_BYTES,
) -> dict[str, Any]:
    """Verify artifact presence, checksums, sizes, and safe source members."""
    bundle_dir = bundle_dir.expanduser().resolve()
    manifest_path = bundle_dir / "manifest.json"
    manifest = _load_json_object(manifest_path)
    if manifest.get("schema_version") != KNOWLEDGE_BUNDLE_SCHEMA_VERSION:
        raise KnowledgeSourceBundleError("Unsupported Knowledge Source bundle schema version")
    source = manifest.get("source")
    if not isinstance(source, dict) or not _is_hex_digest(source.get("commit_sha"), 40):
        raise KnowledgeSourceBundleError("Knowledge Source bundle has invalid source identity")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise KnowledgeSourceBundleError("Knowledge Source bundle has no artifact manifest")

    required = {"graph", "source", "report", "extraction_log"}
    if set(artifacts) != required:
        raise KnowledgeSourceBundleError("Knowledge Source bundle artifact set is incomplete")
    for name in sorted(required):
        entry = artifacts[name]
        if not isinstance(entry, dict):
            raise KnowledgeSourceBundleError(f"Knowledge Source artifact {name!r} has invalid metadata")
        file_name = entry.get("file")
        if not isinstance(file_name, str) or Path(file_name).name != file_name:
            raise KnowledgeSourceBundleError(f"Knowledge Source artifact {name!r} has an unsafe file name")
        path = bundle_dir / file_name
        if not path.is_file():
            raise KnowledgeSourceBundleError(f"Knowledge Source bundle is missing artifact {file_name}")
        if entry.get("size_bytes") != path.stat().st_size:
            raise KnowledgeSourceBundleError(f"Knowledge Source artifact {name!r} size does not match manifest")
        if entry.get("sha256") != _sha256(path):
            raise KnowledgeSourceBundleError(f"Knowledge Source artifact {name!r} checksum does not match manifest")

    source_entry = artifacts["source"]
    expanded_size, file_count = _inspect_source_archive(
        bundle_dir / source_entry["file"],
        max_expanded_bytes=max_expanded_source_bytes,
    )
    if source_entry.get("expanded_size_bytes") != expanded_size or source_entry.get("file_count") != file_count:
        raise KnowledgeSourceBundleError("Knowledge Source archive counts do not match manifest")
    return manifest


def _required_file(output_dir: Path, name: str) -> Path:
    path = output_dir / name
    if not path.is_file():
        raise GraphifyOutputError(f"Graphify output is missing required artifact {name}")
    return path


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphifyOutputError(f"Could not read valid JSON object from {path.name}") from exc
    if not isinstance(value, dict):
        raise GraphifyOutputError(f"{path.name} must contain a JSON object")
    return value


def _required_list(value: dict[str, Any], key: str, path: Path) -> list[Any]:
    items = value.get(key)
    if not isinstance(items, list):
        raise GraphifyOutputError(f"{path.name} field {key!r} must be an array")
    return items


def _validate_relative_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphifyOutputError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if "\\" in value or path.is_absolute() or ".." in path.parts:
        raise GraphifyOutputError(f"{label} is outside the configured source scope")


def _scoped_source_files(
    source_dir: Path,
    include_paths: list[str],
    exclude_paths: list[str],
) -> list[tuple[str, Path]]:
    selected: list[tuple[str, Path]] = []
    for path in source_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative_path = path.relative_to(source_dir).as_posix()
        if ".git" in Path(relative_path).parts:
            continue
        included = not include_paths or any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in include_paths)
        excluded = any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in exclude_paths)
        if included and not excluded:
            selected.append((relative_path, path))
    return sorted(selected)


def _write_source_archive(path: Path, files: list[tuple[str, Path]]) -> int:
    import zstandard

    expanded_size = 0
    with (
        path.open("wb") as compressed,
        zstandard.ZstdCompressor(level=10).stream_writer(compressed, closefd=False) as writer,
        tarfile.open(fileobj=writer, mode="w|") as archive,
    ):
        for relative_path, source_path in files:
            size = source_path.stat().st_size
            info = tarfile.TarInfo(relative_path)
            info.size = size
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with source_path.open("rb") as source_file:
                archive.addfile(info, source_file)
            expanded_size += size
    return expanded_size


def _inspect_source_archive(path: Path, *, max_expanded_bytes: int) -> tuple[int, int]:
    import zstandard

    if max_expanded_bytes <= 0:
        raise ValueError("Expanded source archive limit must be positive")
    expanded_size = 0
    file_count = 0
    with (
        path.open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
        tarfile.open(fileobj=reader, mode="r|") as archive,
    ):
        for member in archive:
            try:
                _validate_relative_path(member.name, f"archive member {member.name!r}")
            except GraphifyOutputError as exc:
                raise KnowledgeSourceBundleError(str(exc)) from exc
            if not member.isfile():
                raise KnowledgeSourceBundleError("Knowledge Source archive may contain only regular files")
            expanded_size += member.size
            file_count += 1
            if expanded_size > max_expanded_bytes:
                raise KnowledgeSourceBundleError("Knowledge Source archive exceeds expanded-size limit")
    return expanded_size, file_count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_hex_digest(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
