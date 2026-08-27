"""Local-only query registry for hydrated Knowledge Source versions."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.knowledge_source_cache import HydratedKnowledgeSource, hydrate_knowledge_source
from shared.lib.models import KnowledgeSource, KnowledgeSourceVersion
from shared.lib.object_store import ObjectStore
from shared.lib.workflow_paths import discover_workflow_packages


class KnowledgeSourceServingError(RuntimeError):
    """Raised when a local Knowledge Source lookup is unavailable or invalid."""


class KnowledgeSourceAccessError(PermissionError):
    """Raised when a workflow is not authorized for a Knowledge Source."""


MAX_SOURCE_SEARCH_QUERY_CHARS = 256
MAX_SOURCE_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_SEARCH_PREVIEW_CHARS = 500
SOURCE_SEARCH_SKIP_DIRS = frozenset({".git", ".serena", ".next", "build", "dist", "node_modules", "target"})


@dataclass(frozen=True)
class ServingSnapshot:
    source_id: uuid.UUID
    version_id: uuid.UUID
    canonical_alias: str
    commit_sha: str
    graph: dict[str, Any]
    source_root: Path


class KnowledgeSourceServingRegistry:
    """Atomically refreshed registry of immutable local graph snapshots."""

    def __init__(
        self,
        *,
        cache_root: Path,
        bucket: str,
        versions_to_keep: int = 2,
        object_store: ObjectStore | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("Knowledge Source object-store bucket is required")
        if versions_to_keep < 2:
            raise ValueError("Knowledge Source serving cache must retain at least two versions")
        self.cache_root = cache_root.expanduser().resolve()
        self.bucket = bucket.strip()
        self.versions_to_keep = versions_to_keep
        self.object_store = object_store
        self._lock = threading.RLock()
        self._by_alias: dict[str, ServingSnapshot] = {}
        self._errors: dict[str, str] = {}
        self._pins: dict[tuple[uuid.UUID, uuid.UUID], int] = {}

    async def refresh(self, session: AsyncSession) -> None:
        """Hydrate promoted registry versions, preserving old snapshots on failure."""
        rows = (
            await session.execute(
                select(KnowledgeSource, KnowledgeSourceVersion)
                .join(
                    KnowledgeSourceVersion,
                    KnowledgeSourceVersion.id == KnowledgeSource.current_successful_version_id,
                )
                .where(KnowledgeSource.enabled.is_(True), KnowledgeSourceVersion.status == "succeeded")
                .order_by(KnowledgeSource.id)
            )
        ).all()
        enabled_aliases = {source.canonical_alias for source, _ in rows}
        replacements: dict[str, ServingSnapshot] = {}
        failures: dict[str, str] = {}
        for source, version in rows:
            try:
                hydrated = await asyncio.to_thread(
                    hydrate_knowledge_source,
                    source,
                    version,
                    cache_root=self.cache_root,
                    bucket=self.bucket,
                    object_store=self.object_store,
                )
                replacements[source.canonical_alias] = _load_snapshot(source, version, hydrated)
            except Exception as exc:
                failures[source.canonical_alias] = str(exc)

        with self._lock:
            for alias in set(self._by_alias) - enabled_aliases:
                self._by_alias.pop(alias, None)
            self._by_alias.update(replacements)
            self._errors = failures
        await asyncio.to_thread(self._cleanup_unpinned_versions)

    def list_sources(self, workflow: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "alias": snapshot.canonical_alias,
                    "commit_sha": snapshot.commit_sha,
                    "version_id": str(snapshot.version_id),
                    "status": "ready",
                }
                for snapshot in sorted(self._by_alias.values(), key=lambda item: item.canonical_alias)
                if _workflow_has_knowledge_mcp(workflow)
            ]

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"ready_sources": len(self._by_alias), "degraded_sources": dict(self._errors)}

    @contextmanager
    def pin(self, alias: str, workflow: str) -> Iterator[ServingSnapshot]:
        with self._lock:
            snapshot = self._by_alias.get(alias)
            if snapshot is None:
                raise KnowledgeSourceServingError(f"Knowledge Source is not available: {alias}")
            if not _workflow_has_knowledge_mcp(workflow):
                raise KnowledgeSourceAccessError(f"Workflow is not authorized for Knowledge Source: {alias}")
            key = (snapshot.source_id, snapshot.version_id)
            self._pins[key] = self._pins.get(key, 0) + 1
        try:
            yield snapshot
        finally:
            with self._lock:
                remaining = self._pins[key] - 1
                if remaining:
                    self._pins[key] = remaining
                else:
                    self._pins.pop(key)

    def _cleanup_unpinned_versions(self) -> None:
        if not self.cache_root.is_dir():
            return
        with self._lock:
            active = {(item.source_id, item.version_id) for item in self._by_alias.values()}
            pinned = set(self._pins)
        for source_dir in self.cache_root.iterdir():
            if not source_dir.is_dir() or source_dir.is_symlink():
                continue
            try:
                source_id = uuid.UUID(source_dir.name)
            except ValueError:
                continue
            versions = sorted(
                (
                    path
                    for path in source_dir.iterdir()
                    if path.is_dir() and not path.is_symlink() and _is_uuid(path.name)
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            retained = 0
            for version_dir in versions:
                version_id = uuid.UUID(version_dir.name)
                key = (source_id, version_id)
                if key in active or key in pinned or retained < self.versions_to_keep:
                    retained += 1
                    continue
                shutil.rmtree(version_dir)


def search_symbols(snapshot: ServingSnapshot, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise ValueError("Symbol query is required")
    bounded_limit = _bounded_limit(limit, maximum=50)
    matches = []
    for node in snapshot.graph["nodes"]:
        searchable = " ".join(
            str(node.get(field, "")) for field in ("id", "name", "label", "qualified_name", "type", "source_file")
        ).casefold()
        if normalized_query in searchable:
            matches.append(_node_result(snapshot, node))
            if len(matches) == bounded_limit:
                break
    if matches:
        return matches

    source_result = search_source(snapshot, query, limit=bounded_limit)
    return [
        {
            "result_type": "source_match",
            "source": source_result["source"],
            "commit_sha": source_result["commit_sha"],
            "query": source_result["query"],
            **match,
        }
        for match in source_result["matches"]
    ]


def search_source(snapshot: ServingSnapshot, query: str, *, limit: int = 20) -> dict[str, Any]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise ValueError("Source query is required")
    if len(query) > MAX_SOURCE_SEARCH_QUERY_CHARS or any(ord(char) < 32 for char in query):
        raise ValueError(f"Source query must be one line of at most {MAX_SOURCE_SEARCH_QUERY_CHARS} characters")
    bounded_limit = _bounded_limit(limit, maximum=50)
    matches = []
    for path in sorted(snapshot.source_root.rglob("*")):
        relative_parts = path.relative_to(snapshot.source_root).parts
        if not path.is_file() or any(part in SOURCE_SEARCH_SKIP_DIRS for part in relative_parts):
            continue
        if path.stat().st_size > MAX_SOURCE_SEARCH_FILE_BYTES:
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        for line_number, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), start=1):
            column = line.casefold().find(normalized_query)
            if column < 0:
                continue
            matches.append(
                {
                    "path": path.relative_to(snapshot.source_root).as_posix(),
                    "line": line_number,
                    "column": column + 1,
                    "preview": _source_search_preview(line, column, len(query)),
                }
            )
            if len(matches) > bounded_limit:
                break
        if len(matches) > bounded_limit:
            break
    return {
        "source": snapshot.canonical_alias,
        "commit_sha": snapshot.commit_sha,
        "query": query,
        "matches": matches[:bounded_limit],
        "count": min(len(matches), bounded_limit),
        "truncated": len(matches) > bounded_limit,
    }


def get_symbol(snapshot: ServingSnapshot, symbol_id: str) -> dict[str, Any]:
    node = _nodes_by_id(snapshot).get(symbol_id)
    if node is None:
        raise KnowledgeSourceServingError(f"Symbol is not present in {snapshot.canonical_alias}: {symbol_id}")
    return _node_result(snapshot, node)


def get_neighbors(
    snapshot: ServingSnapshot,
    symbol_id: str,
    *,
    direction: str = "both",
    limit: int = 50,
) -> dict[str, Any]:
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("Neighbor direction must be incoming, outgoing, or both")
    nodes = _nodes_by_id(snapshot)
    if symbol_id not in nodes:
        raise KnowledgeSourceServingError(f"Symbol is not present in {snapshot.canonical_alias}: {symbol_id}")
    bounded_limit = _bounded_limit(limit, maximum=100)
    neighbors = []
    for link in snapshot.graph["links"]:
        neighbor_id = None
        if direction in {"outgoing", "both"} and link["source"] == symbol_id:
            neighbor_id = link["target"]
        elif direction in {"incoming", "both"} and link["target"] == symbol_id:
            neighbor_id = link["source"]
        if neighbor_id is not None:
            neighbors.append({"relation": _public_link(link), "symbol": _node_result(snapshot, nodes[neighbor_id])})
            if len(neighbors) == bounded_limit:
                break
    return {"symbol": _node_result(snapshot, nodes[symbol_id]), "neighbors": neighbors}


def find_paths(
    snapshot: ServingSnapshot,
    from_symbol_id: str,
    to_symbol_id: str,
    *,
    max_depth: int = 6,
    limit: int = 10,
) -> list[list[dict[str, Any]]]:
    nodes = _nodes_by_id(snapshot)
    if from_symbol_id not in nodes or to_symbol_id not in nodes:
        raise KnowledgeSourceServingError("Both path endpoints must be symbols in the selected source")
    if not 1 <= max_depth <= 12:
        raise ValueError("Path depth must be between 1 and 12")
    bounded_limit = _bounded_limit(limit, maximum=20)
    adjacency: dict[str, list[str]] = {}
    for link in snapshot.graph["links"]:
        adjacency.setdefault(link["source"], []).append(link["target"])
    queue = deque([[from_symbol_id]])
    paths = []
    while queue and len(paths) < bounded_limit:
        path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        for neighbor in adjacency.get(path[-1], []):
            if neighbor in path:
                continue
            candidate = [*path, neighbor]
            if neighbor == to_symbol_id:
                paths.append([_node_result(snapshot, nodes[node_id]) for node_id in candidate])
            else:
                queue.append(candidate)
    return paths


def get_source_excerpt(
    snapshot: ServingSnapshot,
    source_file: str,
    *,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    path = _safe_source_path(snapshot.source_root, source_file)
    if not path.is_file():
        raise KnowledgeSourceServingError(f"Source file is not present in {snapshot.canonical_alias}: {source_file}")
    if start_line < 1 or end_line < start_line or end_line - start_line + 1 > 200:
        raise ValueError("Source excerpt must be a positive range of at most 200 lines")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    actual_end = min(end_line, len(lines))
    return {
        "source": snapshot.canonical_alias,
        "commit_sha": snapshot.commit_sha,
        "path": source_file,
        "start_line": start_line,
        "end_line": actual_end,
        "content": "\n".join(lines[start_line - 1 : actual_end]),
    }


def _load_snapshot(
    source: KnowledgeSource,
    version: KnowledgeSourceVersion,
    hydrated: HydratedKnowledgeSource,
) -> ServingSnapshot:
    graph = json.loads(hydrated.graph_path.read_text(encoding="utf-8"))
    return ServingSnapshot(
        source_id=source.id,
        version_id=version.id,
        canonical_alias=source.canonical_alias,
        commit_sha=version.commit_sha,
        graph=graph,
        source_root=hydrated.source_root,
    )


def _workflow_has_knowledge_mcp(workflow: str) -> bool:
    for package in discover_workflow_packages():
        if package.name != workflow:
            continue
        mcp_config = package.path / ".mcp.json"
        try:
            config = json.loads(mcp_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        servers = config.get("mcpServers")
        return isinstance(servers, dict) and "knowledge" in servers
    return False


def _nodes_by_id(snapshot: ServingSnapshot) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in snapshot.graph["nodes"]}


def _node_result(snapshot: ServingSnapshot, node: dict[str, Any]) -> dict[str, Any]:
    excluded = {"embedding", "vector", "local_path"}
    result = {key: value for key, value in node.items() if key not in excluded}
    result.update({"source": snapshot.canonical_alias, "commit_sha": snapshot.commit_sha})
    return result


def _public_link(link: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in link.items() if key not in {"embedding", "vector"}}


def _source_search_preview(line: str, column: int, query_length: int) -> str:
    if len(line) <= MAX_SOURCE_SEARCH_PREVIEW_CHARS:
        return line.strip()
    start = max(0, column - 120)
    end = min(len(line), max(start + MAX_SOURCE_SEARCH_PREVIEW_CHARS, column + query_length + 120))
    preview = line[start:end].strip()
    return f"{'...' if start else ''}{preview}{'...' if end < len(line) else ''}"


def _safe_source_path(source_root: Path, value: str) -> Path:
    relative = Path(value)
    if not value or "\\" in value or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Source path must be a safe repository-relative path")
    return source_root / relative


def _bounded_limit(value: int, *, maximum: int) -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"Result limit must be between 1 and {maximum}")
    return value


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
