"""Controlled Knowledge MCP backed only by checksum-verified local bundles."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from fastmcp.server.lifespan import lifespan

from mcps.common import bootstrap_platform_env
from shared.lib.config import settings
from shared.lib.db import async_session_factory, ensure_runtime_schema
from shared.lib.knowledge_source_serving import (
    KnowledgeSourceServingRegistry,
)
from shared.lib.knowledge_source_serving import (
    find_paths as find_local_paths,
)
from shared.lib.knowledge_source_serving import (
    get_neighbors as get_local_neighbors,
)
from shared.lib.knowledge_source_serving import (
    get_source_excerpt as get_local_source_excerpt,
)
from shared.lib.knowledge_source_serving import (
    get_symbol as get_local_symbol,
)
from shared.lib.knowledge_source_serving import (
    search_source as search_local_source,
)
from shared.lib.knowledge_source_serving import (
    search_symbols as search_local_symbols,
)

bootstrap_platform_env()
logger = logging.getLogger(__name__)


def _registry_from_settings() -> KnowledgeSourceServingRegistry:
    return KnowledgeSourceServingRegistry(
        cache_root=Path(settings.knowledge_source_serving_cache_root),
        bucket=settings.knowledge_source_object_store_bucket,
        versions_to_keep=int(settings.knowledge_source_cache_versions_to_keep),
    )


registry = _registry_from_settings()


async def _refresh_once() -> None:
    async with async_session_factory() as session:
        await registry.refresh(session)


async def _refresh_loop() -> None:
    interval = int(settings.knowledge_source_refresh_interval_sec)
    if interval <= 0:
        raise ValueError("KNOWLEDGE_SOURCE_REFRESH_INTERVAL_SEC must be positive")
    while True:
        await asyncio.sleep(interval)
        try:
            await _refresh_once()
        except Exception:
            logger.exception("Knowledge Source cache refresh failed")


@lifespan
async def knowledge_lifespan(_server):
    await ensure_runtime_schema()
    await _refresh_once()
    refresh_task = asyncio.create_task(_refresh_loop())
    try:
        yield {"knowledge_registry": registry}
    finally:
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task


mcp = FastMCP("Controlled Knowledge MCP", lifespan=knowledge_lifespan)


def _workflow(headers: dict[str, str]) -> str:
    workflow = headers.get("x-task-workflow", "").strip().lower()
    if not workflow:
        raise PermissionError("Knowledge Source tools require x-task-workflow")
    return workflow


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def list_sources(headers: dict[str, str] = CurrentHeaders()) -> list[dict[str, Any]]:
    """List locally ready Knowledge Sources authorized for the current workflow."""
    return registry.list_sources(_workflow(headers))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def search_symbols(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    query: Annotated[
        str,
        "One exact symbol, filename, code identifier, or literal log substring. Matching is case-insensitive "
        "literal substring search, not semantic or tokenized search; use one anchor per call.",
    ],
    headers: dict[str, str] = CurrentHeaders(),
    limit: Annotated[int, "Maximum results, from 1 through 50."] = 20,
) -> list[dict[str, Any]]:
    """Search symbols, falling back to typed source matches when the graph has no results."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return search_local_symbols(snapshot, query, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def search_source(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    query: Annotated[
        str,
        "One literal source substring from verified evidence, matched case-insensitively. Do not combine unrelated "
        "terms or use an alert title as a semantic query.",
    ],
    headers: dict[str, str] = CurrentHeaders(),
    limit: Annotated[int, "Maximum matching source lines, from 1 through 50."] = 20,
) -> dict[str, Any]:
    """Search one literal anchor in immutable source and return bounded path, line, preview, and commit provenance."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return search_local_source(snapshot, query, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_symbol(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    symbol_id: Annotated[str, "Exact Graphify symbol ID returned by search_symbols."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Get one symbol with repository, commit, and source provenance."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return get_local_symbol(snapshot, symbol_id)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_neighbors(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    symbol_id: Annotated[str, "Exact Graphify symbol ID."],
    headers: dict[str, str] = CurrentHeaders(),
    direction: Annotated[str, "incoming, outgoing, or both."] = "both",
    limit: Annotated[int, "Maximum results, from 1 through 100."] = 50,
) -> dict[str, Any]:
    """Get bounded graph neighbors from one locally pinned source version."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return get_local_neighbors(snapshot, symbol_id, direction=direction, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def find_paths(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    from_symbol_id: Annotated[str, "Exact starting Graphify symbol ID."],
    to_symbol_id: Annotated[str, "Exact target Graphify symbol ID."],
    headers: dict[str, str] = CurrentHeaders(),
    max_depth: Annotated[int, "Maximum directed path depth, from 1 through 12."] = 6,
    limit: Annotated[int, "Maximum paths, from 1 through 20."] = 10,
) -> list[list[dict[str, Any]]]:
    """Find bounded directed paths in one locally pinned source graph."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return find_local_paths(snapshot, from_symbol_id, to_symbol_id, max_depth=max_depth, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_source_excerpt(
    source_alias: Annotated[str, "Approved canonical Knowledge Source alias."],
    source_file: Annotated[str, "Repository-relative source path returned by a symbol lookup."],
    start_line: Annotated[int, "One-based first line."],
    end_line: Annotated[int, "One-based final line, at most 200 lines after start_line."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Read a bounded excerpt from one authorized immutable source snapshot."""
    with registry.pin(source_alias, _workflow(headers)) as snapshot:
        return get_local_source_excerpt(snapshot, source_file, start_line=start_line, end_line=end_line)


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
