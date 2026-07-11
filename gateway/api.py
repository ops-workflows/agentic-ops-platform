"""Control-Plane API — serves data to the UI.

Provides endpoints for:
  - Agent listing and detail
  - Task queue overview
  - Session detail (event timeline replay)
  - Analytics (token usage, duration, tools)
  - Agent provisioning
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import re
import tarfile
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from session_manager.runtime_launchers import get_runtime_launcher
from sqlalchemy import delete, func, select, text, update

from gateway.approval_broker import get_runtime_approval
from gateway.plugin_dir import read_plugin_files
from gateway.scheduler import compute_next_run, register_schedule_job, unregister_schedule_job
from shared.lib.config import settings
from shared.lib.db import async_session_factory
from shared.lib.memory_catalog import BANK_INCIDENT_RCA, BANK_WORKFLOW_LEARNING, load_workflow_banks
from shared.lib.models import Agent, Approval, BackgroundJobRun, Schedule, Session, SessionEvent, Task
from shared.lib.object_store import BUCKET_AGENT_MEMORY, download_bytes, list_objects
from shared.lib.platform_secrets import load_connector_instances
from shared.lib.task_queue import archive_task, count_tasks, list_tasks
from shared.lib.workflow_paths import discover_workflow_packages, find_workflow_package

logger = logging.getLogger(__name__)
router = APIRouter()
REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = REPO_ROOT / "mcps"


# ─── Response Models ─────────────────────────────────────────────────


class AgentResponse(BaseModel):
    id: str
    name: str
    description: str | None
    version: str | None
    provisioned: bool
    paused: bool
    provisioned_at: str | None
    config: dict
    repo_path: str | None
    created: str
    updated: str


class TaskResponse(BaseModel):
    id: str
    workflow: str
    prompt: str
    status: str
    channel: str | None
    metadata: dict
    message_channel: str | None
    message_thread: str | None
    tokens_used: int
    duration_sec: float | None
    error: str | None
    wait_reason: str | None
    wait_deadline: str | None
    archived_at: str | None
    created: str
    updated: str


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    limit: int
    offset: int


class AgentFilesResponse(BaseModel):
    agent_yaml: str
    files: dict[str, str]


class CreateTaskRequest(BaseModel):
    workflow: str
    prompt: str
    channel: str
    metadata: dict[str, Any] | None = None
    message_channel: str | None = None
    message_thread: str | None = None


class SessionDetailResponse(BaseModel):
    id: str
    task_id: str | None
    agent_id: str | None
    status: str
    started: str | None
    ended: str | None
    duration_sec: float | None
    tokens_input: int
    tokens_output: int
    turns: int
    task: TaskResponse | None = None
    tools_used: list
    subagents_used: list
    error: str | None
    events: list[dict]


class AnalyticsResponse(BaseModel):
    total_tasks: int
    succeeded: int
    failed: int
    avg_duration_sec: float | None
    total_tokens: int
    tasks_by_workflow: dict[str, int]
    tokens_by_workflow: dict[str, int]
    tasks_by_status: dict[str, int]
    daily_counts: list[dict]


class ScheduleResponse(BaseModel):
    id: str
    agent_name: str
    schedule_name: str
    cron_expression: str
    prompt: str
    enabled: bool
    last_run_at: str | None
    next_run_at: str | None
    created_at: str


class ScheduleUpdateRequest(BaseModel):
    enabled: bool


class TaskResetResponse(BaseModel):
    status: str
    task: TaskResponse


class TaskDeleteResponse(BaseModel):
    status: str
    task_id: str
    workflow: str
    container_removed: bool


class TaskArchiveResponse(BaseModel):
    status: str
    task: TaskResponse


class ApprovalItemResponse(BaseModel):
    id: str
    task_id: str
    workflow: str | None
    task_status: str | None
    approval_kind: str
    tool_name: str
    status: str
    request_preview: str | None
    reason: str | None
    resolved_by: str | None
    resolved_by_user_id: str | None
    requested_at: str
    resolved_at: str | None
    archived_at: str | None


class PlatformApprovalsResponse(BaseModel):
    counts_by_status: dict[str, int]
    items: list[ApprovalItemResponse]
    total: int
    limit: int
    offset: int


class RuntimeApprovalStatusResponse(BaseModel):
    task_id: str
    tool_name: str
    request_id: str
    status: str
    reason: str | None
    resolved_by: str | None
    resolved_by_user_id: str | None
    requested_at: str
    resolved_at: str | None


class McpToolResponse(BaseModel):
    name: str
    description: str
    read_only: bool
    open_world: bool


class McpServerResponse(BaseModel):
    id: str
    name: str
    description: str
    usage_count: int
    used_by: list[str]
    tools: list[McpToolResponse]


class ConnectorResponse(BaseModel):
    id: str
    name: str
    summary: str
    description: str | None
    source_type: str
    source_label: str
    target_workflow: str | None
    target_channel: str | None
    tags: list[str]
    type: str


class WorkflowRepoResponse(BaseModel):
    source_url: str | None
    source_mode: str
    default_ref: str | None
    pinned_ref: str | None
    last_synced_ref: str | None
    last_synced_commit: str | None
    last_synced_at: str | None
    last_sync_status: str | None
    last_sync_error: str | None
    discovered_workflows: list[str]
    bundle_errors: dict[str, str]


class WorkflowRepoVersionResponse(BaseModel):
    name: str
    commit_sha: str | None


class WorkflowRepoPinRequest(BaseModel):
    ref: str


class HindsightBankResponse(BaseModel):
    bank_id: str
    label: str
    kind: str
    workflows: list[str]
    listed_in_hindsight: bool


class AgentMemoryResponse(BaseModel):
    agent_name: str
    latest_key: str | None
    latest_updated_at: str | None
    version_count: int


class PlatformMemoriesResponse(BaseModel):
    hindsight_available: bool
    hindsight_banks: list[HindsightBankResponse]
    agent_memories: list[AgentMemoryResponse]


class BackgroundJobRunResponse(BaseModel):
    id: str
    job_type: str
    scope: str | None
    status: str
    started_at: str
    finished_at: str | None
    duration_sec: float | None
    summary: dict[str, Any]
    warnings: list[str]
    error: str | None


class PlatformBackgroundJobsResponse(BaseModel):
    items: list[BackgroundJobRunResponse]
    total: int
    limit: int
    offset: int


class HindsightMemoryEntryResponse(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any]
    created_at: str | None


class HindsightDocumentResponse(BaseModel):
    id: str
    created_at: str | None
    updated_at: str | None
    text_length: int
    memory_unit_count: int
    tags: list[str]


class HindsightDirectiveResponse(BaseModel):
    id: str
    name: str
    content: str
    priority: int
    is_active: bool
    tags: list[str]


class HindsightMentalModelResponse(BaseModel):
    id: str
    name: str
    source_query: str | None
    content: str | None
    tags: list[str]
    last_refreshed_at: str | None
    is_stale: bool | None


class HindsightGraphNodeResponse(BaseModel):
    id: str
    label: str
    node_type: str


class HindsightGraphEdgeResponse(BaseModel):
    source: str
    target: str
    edge_type: str
    weight: float | None


class HindsightGraphPreviewResponse(BaseModel):
    nodes: list[HindsightGraphNodeResponse]
    edges: list[HindsightGraphEdgeResponse]
    table_rows: list[dict[str, Any]]
    total_units: int


class HindsightBankStatsResponse(BaseModel):
    total_nodes: int
    total_links: int
    total_documents: int
    total_observations: int
    pending_operations: int
    failed_operations: int
    nodes_by_fact_type: dict[str, int]
    links_by_link_type: dict[str, int]


class HindsightBankDetailResponse(BaseModel):
    bank_id: str
    listed_in_hindsight: bool
    warnings: list[str]
    stats: HindsightBankStatsResponse
    graph: HindsightGraphPreviewResponse | None
    entries: list[HindsightMemoryEntryResponse]


class AgentMemoryFileResponse(BaseModel):
    path: str
    size_bytes: int
    preview: str


class AgentMemoryDetailResponse(BaseModel):
    agent_name: str
    archive_key: str | None
    files: list[AgentMemoryFileResponse]


def _task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=str(task.id),
        workflow=task.workflow,
        prompt=task.prompt,
        status=task.status,
        channel=task.channel,
        metadata=task.task_metadata,
        message_channel=task.message_channel,
        message_thread=task.message_thread,
        tokens_used=task.tokens_used,
        duration_sec=task.duration_sec,
        error=task.error,
        wait_reason=task.wait_reason,
        wait_deadline=task.wait_deadline.isoformat() if task.wait_deadline else None,
        archived_at=task.archived_at.isoformat() if task.archived_at else None,
        created=task.created.isoformat(),
        updated=task.updated.isoformat(),
    )


def _agent_response(agent: Agent) -> AgentResponse:
    return AgentResponse(
        id=str(agent.id),
        name=agent.name,
        description=agent.description,
        version=agent.version,
        provisioned=agent.provisioned,
        paused=agent.paused,
        provisioned_at=agent.provisioned_at.isoformat() if agent.provisioned_at else None,
        config=agent.config,
        repo_path=agent.repo_path,
        created=agent.created.isoformat(),
        updated=agent.updated.isoformat(),
    )


def _stop_and_remove_task_container(container_id: str | None) -> bool:
    if not container_id:
        return False

    try:
        return get_runtime_launcher().cancel(runtime_id=container_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to remove task runtime: {exc}") from exc


def _humanize_identifier(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").strip().title()


def _short_docstring(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    return value.strip().splitlines()[0].strip()


def _mcp_usage_map() -> dict[str, list[str]]:
    usage: defaultdict[str, list[str]] = defaultdict(list)
    for package in discover_workflow_packages():
        config_path = package.path / ".mcp.json"
        if not config_path.exists():
            continue
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            continue

        servers = config.get("mcpServers") or {}
        if not isinstance(servers, dict):
            continue

        for server_id in servers:
            usage[server_id].append(package.name)

    return {server_id: sorted(workflows) for server_id, workflows in usage.items()}


def _enabled_catalog_ids(section_name: str) -> set[str] | None:
    platform_file = settings.platform_config_file or settings.platform_secrets_file
    if not platform_file:
        return None
    path = Path(platform_file)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.warning("Failed to parse platform config catalog section from %s", path)
        return None
    section = data.get(section_name) or {}
    if not isinstance(section, dict) or "enabled" not in section:
        return None
    enabled = section.get("enabled")
    if enabled is None:
        return set()
    if not isinstance(enabled, list):
        logger.warning("Ignoring non-list %s.enabled in %s", section_name, path)
        return None
    return {str(item).strip() for item in enabled if str(item).strip()}


def _read_mcp_catalog() -> list[McpServerResponse]:
    usage = _mcp_usage_map()
    enabled_ids = _enabled_catalog_ids("mcps")
    servers: list[McpServerResponse] = []

    for path in sorted(MCP_DIR.glob("*/mcp_*.py")):
        module = ast.parse(path.read_text(), filename=str(path))
        server_id = path.stem.removeprefix("mcp_")
        if enabled_ids is not None and server_id not in enabled_ids:
            continue
        server_name = _humanize_identifier(server_id)

        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            if node.targets[0].id != "mcp":
                continue
            if isinstance(node.value, ast.Call) and node.value.args:
                first_arg = node.value.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    server_name = first_arg.value
                break

        tools: list[McpToolResponse] = []
        for node in module.body:
            if not isinstance(node, ast.FunctionDef):
                continue

            read_only = False
            open_world = False
            exposed = False
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "tool":
                    continue
                if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "mcp":
                    continue

                exposed = True
                for keyword in decorator.keywords:
                    if keyword.arg != "annotations" or not isinstance(keyword.value, ast.Dict):
                        continue
                    for key_node, value_node in zip(keyword.value.keys, keyword.value.values, strict=False):
                        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                            continue
                        if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, bool):
                            continue
                        if key_node.value == "readOnlyHint":
                            read_only = value_node.value
                        if key_node.value == "openWorldHint":
                            open_world = value_node.value

            if not exposed:
                continue

            tools.append(
                McpToolResponse(
                    name=node.name,
                    description=_short_docstring(ast.get_docstring(node), _humanize_identifier(node.name)),
                    read_only=read_only,
                    open_world=open_world,
                )
            )

        module_description = _short_docstring(ast.get_docstring(module), f"{server_name} tool surface")
        used_by = usage.get(server_id, [])
        servers.append(
            McpServerResponse(
                id=server_id,
                name=server_name,
                description=module_description,
                usage_count=len(used_by),
                used_by=used_by,
                tools=tools,
            )
        )

    return servers


def _connector_source_label(config: dict[str, Any]) -> str:
    metadata = config.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("source_label"):
        return str(metadata["source_label"])

    source = config.get("source") or {}
    if isinstance(source, dict):
        if source.get("subscription"):
            return f"Pub/Sub subscription {source['subscription']}"
        if source.get("table"):
            return f"ServiceNow {source['table']} table"
        if source.get("payload"):
            return str(source["payload"])

    return _humanize_identifier(str(config.get("name") or "connector"))


def _read_connectors_catalog() -> list[ConnectorResponse]:
    platform_file = settings.platform_config_file or settings.platform_secrets_file
    if not platform_file:
        return []
    instances = load_connector_instances(platform_file)
    enabled_ids = _enabled_catalog_ids("connectors")

    connectors: list[ConnectorResponse] = []
    for instance_id, config in sorted(instances.items()):
        if enabled_ids is not None and instance_id not in enabled_ids:
            continue

        metadata = config.get("metadata") or {}
        tags = metadata.get("tags") if isinstance(metadata, dict) else None
        tag_values = [str(tag) for tag in tags] if isinstance(tags, list) else []
        description = str(config.get("description") or "") or None
        summary = (
            str(metadata.get("summary"))
            if isinstance(metadata, dict) and metadata.get("summary")
            else (description or f"{_humanize_identifier(instance_id)} integration")
        )
        source = config.get("source") or {}
        target = config.get("target") or {}
        metadata_display_name = metadata.get("display_name") if isinstance(metadata, dict) else None
        display_name = config.get("display_name") or metadata_display_name

        connectors.append(
            ConnectorResponse(
                id=instance_id,
                name=str(display_name or instance_id),
                summary=summary,
                description=description,
                source_type=str(source.get("type") or "unknown"),
                source_label=_connector_source_label(config),
                target_workflow=str(target.get("workflow") or "") or None,
                target_channel=str(target.get("message_channel") or "") or None,
                tags=tag_values,
                type=str(config.get("type") or source.get("type") or "unknown"),
            )
        )

    return connectors


async def _hindsight_request_json_async(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> tuple[Any | None, str | None]:
    url = f"{settings.hindsight_url}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, params=params, json=json_body)
            response.raise_for_status()
            return response.json(), None
    except ValueError as exc:
        logger.warning("Hindsight returned invalid JSON for %s %s: %s", method, url, exc)
        return None, f"{method} {endpoint} returned invalid JSON"
    except httpx.HTTPStatusError as exc:
        logger.warning("Hindsight returned HTTP %s for %s %s", exc.response.status_code, method, url)
        return None, f"{method} {endpoint} returned HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        logger.warning("Hindsight request failed for %s %s: %s", method, url, exc)
        return None, f"{method} {endpoint} failed"


async def _list_hindsight_banks_async() -> tuple[set[str], list[str]]:
    payload, error = await _hindsight_request_json_async("GET", "/v1/default/banks", timeout=5.0)
    if error:
        return set(), [error]

    banks = payload.get("banks") if isinstance(payload, dict) else None
    if not isinstance(banks, list):
        return set(), ["GET /v1/default/banks returned an unexpected payload"]

    bank_ids = {
        str(item.get("bank_id") or "").strip()
        for item in banks
        if isinstance(item, dict) and str(item.get("bank_id") or "").strip()
    }
    return bank_ids, []


def _hindsight_banks_catalog(live_bank_ids: set[str] | None = None) -> list[HindsightBankResponse]:
    grouped: dict[tuple[str, str], set[str]] = {}
    defaults = {
        (BANK_INCIDENT_RCA, "business"): set(),
        (BANK_WORKFLOW_LEARNING, "learning"): set(),
    }

    for kind, mapping in load_workflow_banks().items():
        for workflow, bank_id in mapping.items():
            grouped.setdefault((bank_id, kind), set()).add(workflow)

    grouped.update({key: grouped.get(key, set()) | value for key, value in defaults.items()})

    banks: list[HindsightBankResponse] = []
    for (bank_id, kind), workflows in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0])):
        banks.append(
            HindsightBankResponse(
                bank_id=bank_id,
                label=_humanize_identifier(bank_id),
                kind=kind,
                workflows=sorted(workflows),
                listed_in_hindsight=bank_id in (live_bank_ids or set()),
            )
        )

    known_ids = {bank.bank_id for bank in banks}
    for bank_id in sorted((live_bank_ids or set()) - known_ids):
        banks.append(
            HindsightBankResponse(
                bank_id=bank_id,
                label=_humanize_identifier(bank_id),
                kind="discovered",
                workflows=[],
                listed_in_hindsight=True,
            )
        )

    return banks


async def _hindsight_available_async() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.hindsight_url}/health")
            response.raise_for_status()
            return True
    except httpx.HTTPError:
        return False


def _extract_hindsight_entries(payload: Any, limit: int) -> list[HindsightMemoryEntryResponse]:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("items", "memories", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
    elif isinstance(payload, list):
        candidates.extend(payload)

    entries: list[HindsightMemoryEntryResponse] = []
    for idx, item in enumerate(candidates):
        if len(entries) >= limit:
            break
        if isinstance(item, dict):
            content = (
                str(item.get("content") or item.get("text") or item.get("memory") or item.get("summary") or "")
            ).strip()
            raw_metadata = item.get("metadata")
            metadata: dict[str, Any] = (
                {str(key): value for key, value in raw_metadata.items()} if isinstance(raw_metadata, dict) else {}
            )
            for key in ("type", "context", "entities", "document_id", "tags"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    metadata[key] = value
            created = (
                item.get("date")
                or item.get("mentioned_at")
                or item.get("created_at")
                or item.get("timestamp")
                or item.get("updated_at")
            )
            entry_id = str(item.get("id") or item.get("memory_id") or f"entry-{idx + 1}")
        else:
            content = str(item).strip()
            metadata = {}
            created = None
            entry_id = f"entry-{idx + 1}"

        if not content:
            continue
        entries.append(
            HindsightMemoryEntryResponse(
                id=entry_id,
                content=content,
                metadata=metadata,
                created_at=str(created) if created else None,
            )
        )
    return entries


async def _fetch_hindsight_entries_async(
    bank_id: str, limit: int
) -> tuple[list[HindsightMemoryEntryResponse], list[str]]:
    payload, error = await _hindsight_request_json_async(
        "GET",
        f"/v1/default/banks/{bank_id}/memories/list",
        params={"limit": limit, "offset": 0},
    )
    if error:
        return [], [error]
    return _extract_hindsight_entries(payload, limit), []


async def _fetch_hindsight_stats_async(bank_id: str) -> tuple[HindsightBankStatsResponse, list[str]]:
    payload, error = await _hindsight_request_json_async("GET", f"/v1/default/banks/{bank_id}/stats")
    empty = HindsightBankStatsResponse(
        total_nodes=0,
        total_links=0,
        total_documents=0,
        total_observations=0,
        pending_operations=0,
        failed_operations=0,
        nodes_by_fact_type={},
        links_by_link_type={},
    )
    if error:
        return empty, [error]
    if not isinstance(payload, dict):
        return empty, [f"GET /v1/default/banks/{bank_id}/stats returned an unexpected payload"]
    return (
        HindsightBankStatsResponse(
            total_nodes=int(payload.get("total_nodes") or 0),
            total_links=int(payload.get("total_links") or 0),
            total_documents=int(payload.get("total_documents") or 0),
            total_observations=int(payload.get("total_observations") or 0),
            pending_operations=int(payload.get("pending_operations") or 0),
            failed_operations=int(payload.get("failed_operations") or 0),
            nodes_by_fact_type={
                str(key): int(value) for key, value in (payload.get("nodes_by_fact_type") or {}).items()
            },
            links_by_link_type={
                str(key): int(value) for key, value in (payload.get("links_by_link_type") or {}).items()
            },
        ),
        [],
    )


async def _fetch_hindsight_graph_async(
    bank_id: str, limit: int
) -> tuple[HindsightGraphPreviewResponse | None, list[str]]:
    payload, error = await _hindsight_request_json_async(
        "GET",
        f"/v1/default/banks/{bank_id}/graph",
        params={"limit": limit},
    )
    if error:
        return None, [error]
    if not isinstance(payload, dict):
        return None, [f"GET /v1/default/banks/{bank_id}/graph returned an unexpected payload"]

    raw_nodes_value = payload.get("nodes")
    raw_edges_value = payload.get("edges")
    table_rows_value = payload.get("table_rows")
    raw_nodes: list[Any] = raw_nodes_value if isinstance(raw_nodes_value, list) else []
    raw_edges: list[Any] = raw_edges_value if isinstance(raw_edges_value, list) else []
    table_rows: list[Any] = table_rows_value if isinstance(table_rows_value, list) else []

    nodes: list[HindsightGraphNodeResponse] = []
    for idx, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            continue
        nodes.append(
            HindsightGraphNodeResponse(
                id=str(node.get("id") or f"node-{idx + 1}"),
                label=str(node.get("label") or node.get("text") or node.get("id") or f"Node {idx + 1}"),
                node_type=str(node.get("type") or "memory"),
            )
        )

    edges: list[HindsightGraphEdgeResponse] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") or edge.get("source")
        target = edge.get("to") or edge.get("target")
        if not source or not target:
            continue
        weight = edge.get("weight")
        edges.append(
            HindsightGraphEdgeResponse(
                source=str(source),
                target=str(target),
                edge_type=str(edge.get("type") or "link"),
                weight=float(weight) if isinstance(weight, (int, float)) else None,
            )
        )

    return (
        HindsightGraphPreviewResponse(
            nodes=nodes,
            edges=edges,
            table_rows=[row for row in table_rows if isinstance(row, dict)][: min(limit, 12)],
            total_units=int(payload.get("total_units") or 0),
        ),
        [],
    )


def _is_text_memory_file(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {".md", ".txt", ".json", ".yaml", ".yml", ".log", ".cfg", ".conf"}


def _preview_agent_memory_files(
    archive_bytes: bytes, *, max_files: int, max_preview_chars: int
) -> list[AgentMemoryFileResponse]:
    files: list[AgentMemoryFileResponse] = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if len(files) >= max_files:
                break
            if not member.isfile() or member.size <= 0:
                continue
            if not _is_text_memory_file(member.name):
                continue

            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            raw = extracted.read(min(member.size, 128 * 1024))
            preview = raw.decode("utf-8", errors="replace").strip()
            if not preview:
                continue

            files.append(
                AgentMemoryFileResponse(
                    path=member.name,
                    size_bytes=int(member.size),
                    preview=preview[:max_preview_chars],
                )
            )

    return files


def _agent_memories_catalog() -> list[AgentMemoryResponse]:
    try:
        objects = list_objects(BUCKET_AGENT_MEMORY)
    except Exception:
        logger.exception("Failed to list agent memory objects")
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for item in objects:
        key = item.key
        if not key or "/" not in key:
            continue
        agent_name = key.split("/", 1)[0]
        item_last_modified = item.last_modified
        entry = grouped.setdefault(
            agent_name,
            {
                "latest_key": None,
                "latest_updated_at": None,
                "latest_updated_at_dt": None,
                "version_count": 0,
            },
        )
        entry["version_count"] += 1
        item_updated = item_last_modified.isoformat() if item_last_modified else None
        previous_last_modified = entry["latest_updated_at_dt"]
        is_newer = bool(
            item_last_modified and (previous_last_modified is None or item_last_modified > previous_last_modified)
        )
        if key.endswith("/latest.tar.gz") or previous_last_modified is None or is_newer:
            entry["latest_key"] = key
            entry["latest_updated_at"] = item_updated
            entry["latest_updated_at_dt"] = item_last_modified

    return [
        AgentMemoryResponse(
            agent_name=agent_name,
            latest_key=data["latest_key"],
            latest_updated_at=data["latest_updated_at"],
            version_count=int(data["version_count"]),
        )
        for agent_name, data in sorted(grouped.items())
    ]


def _background_job_run_response(run: BackgroundJobRun) -> BackgroundJobRunResponse:
    return BackgroundJobRunResponse(
        id=str(run.id),
        job_type=run.job_type,
        scope=run.scope,
        status=run.status,
        started_at=run.started_at.isoformat(),
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        duration_sec=run.duration_sec,
        summary=run.summary or {},
        warnings=[str(item) for item in (run.warnings or [])],
        error=run.error,
    )


# ─── Agents ──────────────────────────────────────────────────────────


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(provisioned: bool | None = None):
    async with async_session_factory() as session:
        query = select(Agent).order_by(Agent.name)
        if provisioned is None:
            query = query.where(Agent.provisioned.is_(True))
        else:
            query = query.where(Agent.provisioned == provisioned)
        result = await session.execute(query)
        agents = result.scalars().all()

    return [_agent_response(a) for a in agents]


@router.get("/agents/{name}", response_model=AgentResponse)
async def get_agent(name: str):
    async with async_session_factory() as session:
        result = await session.execute(select(Agent).where(Agent.name == name, Agent.provisioned.is_(True)))
        agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return _agent_response(agent)


@router.get("/agents/{name}/files", response_model=AgentFilesResponse)
async def get_agent_files(name: str):
    _, path = _load_agent_yaml(name)
    plugin_dir = Path(path).parent
    files = read_plugin_files(plugin_dir)
    agent_yaml = files.pop("agent.yaml", Path(path).read_text())
    return AgentFilesResponse(agent_yaml=agent_yaml, files=files)


@router.post("/agents/{name}/provision")
async def provision_agent(name: str):
    async with async_session_factory() as session:
        result = await session.execute(select(Agent).where(Agent.name == name))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        agent.provisioned = True
        agent.provisioned_at = datetime.now(UTC)
        await session.commit()
    return {"status": "provisioned", "agent": name}


@router.post("/agents/{name}/pause", response_model=AgentResponse)
async def pause_agent(name: str):
    async with async_session_factory() as session:
        result = await session.execute(select(Agent).where(Agent.name == name, Agent.provisioned.is_(True)))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        agent.paused = True
        agent.updated = datetime.now(UTC)
        await session.commit()
        await session.refresh(agent)

    return _agent_response(agent)


@router.post("/agents/{name}/resume", response_model=AgentResponse)
async def resume_agent(name: str):
    async with async_session_factory() as session:
        result = await session.execute(select(Agent).where(Agent.name == name, Agent.provisioned.is_(True)))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        agent.paused = False
        agent.updated = datetime.now(UTC)
        await session.commit()
        await session.refresh(agent)

    return _agent_response(agent)


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules():
    async with async_session_factory() as session:
        result = await session.execute(
            select(Schedule, Agent)
            .join(Agent, Schedule.agent_id == Agent.id)
            .where(Agent.provisioned.is_(True))
            .order_by(Agent.name, Schedule.name)
        )
        rows = result.all()
        responses: list[ScheduleResponse] = []
        for schedule, agent in rows:
            last_run = schedule.last_run
            if last_run is None:
                last_run_result = await session.execute(
                    select(func.max(Task.created)).where(
                        Task.workflow == agent.name,
                        Task.task_metadata["schedule"].astext == schedule.name,
                        Task.task_metadata["triggered_by"].astext == "scheduler",
                    )
                )
                last_run = last_run_result.scalar_one_or_none()

            responses.append(
                ScheduleResponse(
                    id=str(schedule.id),
                    agent_name=agent.name,
                    schedule_name=schedule.name,
                    cron_expression=schedule.cron,
                    prompt=schedule.prompt or "",
                    enabled=schedule.enabled,
                    last_run_at=last_run.isoformat() if last_run else None,
                    next_run_at=(
                        schedule.next_run.isoformat() if schedule.next_run else compute_next_run(schedule.cron)
                    ),
                    created_at=schedule.created.isoformat(),
                )
            )

    return responses


@router.put("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(schedule_id: str, body: ScheduleUpdateRequest):
    schedule_uuid = uuid.UUID(schedule_id)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Schedule, Agent)
            .join(Agent, Schedule.agent_id == Agent.id)
            .where(Schedule.id == schedule_uuid, Agent.provisioned.is_(True))
        )
        row = result.one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Schedule not found")

        schedule, agent = row
        schedule.enabled = body.enabled
        schedule.next_run = (
            datetime.fromisoformat(next_run) if body.enabled and (next_run := compute_next_run(schedule.cron)) else None
        )
        await session.commit()
        await session.refresh(schedule)

    if body.enabled:
        register_schedule_job(
            agent_name=agent.name,
            schedule_name=schedule.name,
            cron=schedule.cron,
            prompt=schedule.prompt or "",
            agent_config=agent.config or {},
        )
    else:
        unregister_schedule_job(agent_name=agent.name, schedule_name=schedule.name)

    return ScheduleResponse(
        id=str(schedule.id),
        agent_name=agent.name,
        schedule_name=schedule.name,
        cron_expression=schedule.cron,
        prompt=schedule.prompt or "",
        enabled=schedule.enabled,
        last_run_at=schedule.last_run.isoformat() if schedule.last_run else None,
        next_run_at=schedule.next_run.isoformat() if schedule.next_run else None,
        created_at=schedule.created.isoformat(),
    )


# ─── Tasks ───────────────────────────────────────────────────────────


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks_api(
    workflow: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    search: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    include_archived: bool = False,
    sort_by: str = "created",
    sort_dir: str = "desc",
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        parsed_created_after = datetime.fromisoformat(created_after) if created_after else None
        parsed_created_before = datetime.fromisoformat(created_before) if created_before else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="created_after/created_before must be ISO datetimes") from exc
    async with async_session_factory() as session:
        tasks = await list_tasks(
            session,
            workflow=workflow,
            status=status,
            channel=channel,
            search=search,
            created_after=parsed_created_after,
            created_before=parsed_created_before,
            include_archived=include_archived,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        total = await count_tasks(
            session,
            workflow=workflow,
            status=status,
            channel=channel,
            search=search,
            created_after=parsed_created_after,
            created_before=parsed_created_before,
            include_archived=include_archived,
        )

    return TaskListResponse(
        items=[_task_response(t) for t in tasks],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task_api(task_id: str):
    async with async_session_factory() as session:
        result = await session.execute(select(Task).where(Task.id == uuid.UUID(task_id)))
        task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(task)


@router.post("/tasks", response_model=TaskResponse)
async def create_task_api(body: CreateTaskRequest):
    from shared.lib.task_queue import create_task

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=body.workflow,
            prompt=body.prompt,
            channel=body.channel,
            metadata=body.metadata,
            message_channel=body.message_channel,
            message_thread=body.message_thread,
        )

    return _task_response(task)


@router.post("/tasks/{task_id}/rerun", response_model=TaskResetResponse)
async def rerun_task_api(task_id: str):
    task_uuid = uuid.UUID(task_id)
    rerunnable_statuses = {"failed", "lost", "timed_out"}

    async with async_session_factory() as session:
        result = await session.execute(select(Task).where(Task.id == task_uuid))
        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in rerunnable_statuses:
            raise HTTPException(
                status_code=409,
                detail=f"Only {', '.join(sorted(rerunnable_statuses))} tasks can be rerun",
            )

        await session.execute(delete(SessionEvent).where(SessionEvent.task_id == task_uuid))
        await session.execute(delete(Session).where(Session.task_id == task_uuid))

        await session.execute(
            update(Task)
            .where(Task.id == task_uuid)
            .values(
                status="queued",
                session_id=None,
                container_id=None,
                heartbeat=None,
                result=None,
                tokens_used=0,
                duration_sec=None,
                error=None,
                updated=datetime.now(UTC),
            )
        )

        await session.commit()
        await session.refresh(task)

    return TaskResetResponse(
        status="queued",
        task=_task_response(task),
    )


@router.delete("/tasks/{task_id}", response_model=TaskDeleteResponse)
async def delete_task_api(task_id: str):
    task_uuid = uuid.UUID(task_id)

    async with async_session_factory() as session:
        result = await session.execute(select(Task).where(Task.id == task_uuid))
        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        workflow = task.workflow
        container_id = task.container_id

    container_removed = _stop_and_remove_task_container(container_id)

    async with async_session_factory() as session:
        await session.execute(delete(SessionEvent).where(SessionEvent.task_id == task_uuid))
        await session.execute(delete(Session).where(Session.task_id == task_uuid))
        await session.execute(delete(Task).where(Task.id == task_uuid))
        await session.commit()

    return TaskDeleteResponse(
        status="deleted",
        task_id=task_id,
        workflow=workflow,
        container_removed=container_removed,
    )


@router.post("/tasks/{task_id}/archive", response_model=TaskArchiveResponse)
async def archive_task_api(task_id: str):
    task_uuid = uuid.UUID(task_id)
    async with async_session_factory() as session:
        task = await session.get(Task, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        await archive_task(session, task_uuid, archived=True)
        await session.refresh(task)
    return TaskArchiveResponse(status="archived", task=_task_response(task))


@router.post("/tasks/{task_id}/unarchive", response_model=TaskArchiveResponse)
async def unarchive_task_api(task_id: str):
    task_uuid = uuid.UUID(task_id)
    async with async_session_factory() as session:
        task = await session.get(Task, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        await archive_task(session, task_uuid, archived=False)
        await session.refresh(task)
    return TaskArchiveResponse(status="active", task=_task_response(task))


# ─── Sessions ────────────────────────────────────────────────────────


@router.get("/sessions/{task_id}", response_model=SessionDetailResponse)
async def get_session_detail(task_id: str):
    task_uuid = uuid.UUID(task_id)
    async with async_session_factory() as session:
        task_result = await session.execute(select(Task).where(Task.id == task_uuid))
        task = task_result.scalar_one_or_none()

        # Get session
        sess_result = await session.execute(select(Session).where(Session.task_id == task_uuid))
        sess = sess_result.scalar_one_or_none()

        # Get events
        events_result = await session.execute(
            select(SessionEvent).where(SessionEvent.task_id == task_uuid).order_by(SessionEvent.timestamp)
        )
        events = events_result.scalars().all()

    if not sess and not events:
        raise HTTPException(status_code=404, detail="Session not found")

    # Build response even when Session record is missing (e.g. task ran
    # outside the session manager but events were still collected).
    return SessionDetailResponse(
        id=str(sess.id) if sess else str(task_uuid),
        task_id=str(sess.task_id) if sess and sess.task_id else str(task_uuid),
        agent_id=str(sess.agent_id) if sess and sess.agent_id else None,
        status=sess.status if sess else (task.status if task else "unknown"),
        started=(
            sess.started.isoformat() if sess and sess.started else (events[0].timestamp.isoformat() if events else None)
        ),
        ended=sess.ended.isoformat() if sess and sess.ended else (events[-1].timestamp.isoformat() if events else None),
        duration_sec=sess.duration_sec if sess else (task.duration_sec if task else None),
        tokens_input=sess.tokens_input if sess else None,
        tokens_output=sess.tokens_output if sess else None,
        turns=sess.turns if sess else None,
        task=(_task_response(task) if task else None),
        tools_used=sess.tools_used if sess else [],
        subagents_used=sess.subagents_used if sess else [],
        error=sess.error if sess else (task.error if task else None),
        events=[
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "timestamp": e.timestamp.isoformat(),
                "data": e.data,
            }
            for e in events
        ],
    )


# ─── Analytics ───────────────────────────────────────────────────────


@router.get("/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    days: int = Query(default=7, le=90),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
):
    if start_date and end_date:
        cutoff = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=UTC, hour=23, minute=59, second=59)
    else:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        end_dt = datetime.now(UTC)

    async with async_session_factory() as db:
        date_filter = [Task.created >= cutoff, Task.created <= end_dt]

        # Total counts
        total = await db.execute(select(func.count(Task.id)).where(*date_filter))
        total_count = total.scalar() or 0

        succeeded = await db.execute(select(func.count(Task.id)).where(Task.status == "succeeded", *date_filter))
        succeeded_count = succeeded.scalar() or 0

        failed = await db.execute(select(func.count(Task.id)).where(Task.status == "failed", *date_filter))
        failed_count = failed.scalar() or 0

        # Average duration
        avg_dur = await db.execute(
            select(func.avg(Task.duration_sec)).where(*date_filter, Task.duration_sec.isnot(None))
        )
        avg_duration = avg_dur.scalar()

        # Total tokens
        total_tok = await db.execute(select(func.sum(Task.tokens_used)).where(*date_filter))
        total_tokens = total_tok.scalar() or 0

        # By workflow
        wf_result = await db.execute(
            select(Task.workflow, func.count(Task.id)).where(*date_filter).group_by(Task.workflow)
        )
        by_workflow = {row[0]: row[1] for row in wf_result}

        # Tokens by workflow
        tok_wf_result = await db.execute(
            select(Task.workflow, func.coalesce(func.sum(Task.tokens_used), 0))
            .where(*date_filter)
            .group_by(Task.workflow)
        )
        tokens_by_workflow = {row[0]: row[1] for row in tok_wf_result}

        # By status
        st_result = await db.execute(select(Task.status, func.count(Task.id)).where(*date_filter).group_by(Task.status))
        by_status = {row[0]: row[1] for row in st_result}

        # Daily counts
        daily_result = await db.execute(
            text("""
                SELECT date_trunc('day', created)::date as day, count(*)
                FROM task_queue.tasks
                WHERE created >= :cutoff AND created <= :end_dt
                GROUP BY day ORDER BY day
            """),
            {"cutoff": cutoff, "end_dt": end_dt},
        )
        daily_counts = [{"date": str(row[0]), "count": row[1]} for row in daily_result]

    return AnalyticsResponse(
        total_tasks=total_count,
        succeeded=succeeded_count,
        failed=failed_count,
        avg_duration_sec=round(avg_duration, 2) if avg_duration else None,
        total_tokens=total_tokens,
        tasks_by_workflow=by_workflow,
        tokens_by_workflow=tokens_by_workflow,
        tasks_by_status=by_status,
        daily_counts=daily_counts,
    )


@router.get("/platform/approvals", response_model=PlatformApprovalsResponse)
async def get_platform_approvals(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    include_archived: bool = False,
):
    async with async_session_factory() as session:
        counts_query = select(Approval.status, func.count(Approval.id)).group_by(Approval.status)
        approvals_query = select(Approval, Task.status).join(Task, Task.id == Approval.task_id)
        total_query = select(func.count(Approval.id))
        if not include_archived:
            counts_query = counts_query.where(Approval.archived_at.is_(None))
            approvals_query = approvals_query.where(Approval.archived_at.is_(None))
            total_query = total_query.where(Approval.archived_at.is_(None))
        counts_result = await session.execute(counts_query)
        approvals_result = await session.execute(
            approvals_query.order_by(Approval.requested_at.desc()).limit(limit).offset(offset)
        )
        total = int((await session.execute(total_query)).scalar() or 0)

    counts = {row[0]: row[1] for row in counts_result}
    items = [
        ApprovalItemResponse(
            id=str(approval.id),
            task_id=str(approval.task_id),
            workflow=approval.workflow,
            task_status=task_status,
            approval_kind=approval.approval_kind,
            tool_name=approval.tool_name,
            status=approval.status,
            request_preview=approval.request_preview,
            reason=approval.reason,
            resolved_by=(approval.approval_metadata or {}).get("approval_result", {}).get("approved_by"),
            resolved_by_user_id=(approval.approval_metadata or {})
            .get("approval_result", {})
            .get("approved_by_user_id"),
            requested_at=approval.requested_at.isoformat(),
            resolved_at=approval.resolved_at.isoformat() if approval.resolved_at else None,
            archived_at=approval.archived_at.isoformat() if approval.archived_at else None,
        )
        for approval, task_status in approvals_result.all()
    ]
    return PlatformApprovalsResponse(counts_by_status=counts, items=items, total=total, limit=limit, offset=offset)


@router.get("/runtime/approvals/status", response_model=RuntimeApprovalStatusResponse)
async def get_runtime_approval_status(task_id: str, tool_name: str, request_id: str):
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid task_id") from exc

    async with async_session_factory() as session:
        approval = await get_runtime_approval(
            session,
            task_id=task_uuid,
            tool_name=tool_name,
            request_id=request_id,
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="Approval not found")

    approval_result = (approval.approval_metadata or {}).get("approval_result", {})
    return RuntimeApprovalStatusResponse(
        task_id=str(approval.task_id),
        tool_name=approval.tool_name,
        request_id=request_id,
        status=approval.status,
        reason=approval.reason,
        resolved_by=approval_result.get("approved_by"),
        resolved_by_user_id=approval_result.get("approved_by_user_id"),
        requested_at=approval.requested_at.isoformat(),
        resolved_at=approval.resolved_at.isoformat() if approval.resolved_at else None,
    )


@router.get("/platform/mcp", response_model=list[McpServerResponse])
async def get_platform_mcp_catalog():
    return _read_mcp_catalog()


@router.get("/platform/connectors", response_model=list[ConnectorResponse])
async def get_platform_connectors():
    return _read_connectors_catalog()


_GITHUB_HTTPS_REPO_PATTERN = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


async def _fetch_github_tags(repo_url: str) -> list[WorkflowRepoVersionResponse]:
    """List released tags for a github.com https repo URL.

    Other git hosts are not supported yet — this returns an empty list
    rather than failing, since version listing is a UI convenience and
    should not block sync/pin operations.
    """
    match = _GITHUB_HTTPS_REPO_PATTERN.match(repo_url)
    if not match:
        logger.info("Workflow repo version listing only supports github.com https URLs; got %r", repo_url)
        return []

    owner, repo = match.group(1), match.group(2)
    headers = {"Accept": "application/vnd.github+json"}
    pat = settings.workflow_repo_pat.strip()
    if pat:
        headers["Authorization"] = f"Bearer {pat}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/tags", headers=headers)
            response.raise_for_status()
            tags = response.json()
    except httpx.HTTPError:
        logger.exception("Failed to list workflow repo tags from GitHub")
        return []

    if not isinstance(tags, list):
        return []
    return [
        WorkflowRepoVersionResponse(name=str(tag["name"]), commit_sha=(tag.get("commit") or {}).get("sha"))
        for tag in tags
        if isinstance(tag, dict) and tag.get("name")
    ]


def _workflow_repo_response(state: Any | None) -> WorkflowRepoResponse:
    source_url = settings.workflow_repo_url.strip() or None
    return WorkflowRepoResponse(
        source_url=source_url,
        source_mode="remote" if source_url else "local",
        default_ref=settings.workflow_repo_ref.strip() or None,
        pinned_ref=state.pinned_ref if state else None,
        last_synced_ref=state.last_synced_ref if state else None,
        last_synced_commit=state.last_synced_commit if state else None,
        last_synced_at=state.last_synced_at.isoformat() if state and state.last_synced_at else None,
        last_sync_status=state.last_sync_status if state else None,
        last_sync_error=state.last_sync_error if state else None,
        discovered_workflows=list(state.discovered_workflows) if state else [],
        bundle_errors=dict(state.bundle_errors) if state else {},
    )


@router.get("/platform/workflow-repo", response_model=WorkflowRepoResponse)
async def get_workflow_repo_status():
    from shared.lib.workflow_repo_sync import get_workflow_repo_state

    async with async_session_factory() as session:
        state = await get_workflow_repo_state(session)
    return _workflow_repo_response(state)


@router.post("/platform/workflow-repo/sync", response_model=WorkflowRepoResponse)
async def trigger_workflow_repo_sync():
    from shared.lib.workflow_repo_sync import sync_workflow_repo

    async with async_session_factory() as session:
        await sync_workflow_repo(session)
    return await get_workflow_repo_status()


@router.post("/platform/workflow-repo/pin", response_model=WorkflowRepoResponse)
async def pin_workflow_repo_version(payload: WorkflowRepoPinRequest):
    from shared.lib.workflow_repo_sync import pin_workflow_repo_ref

    ref = payload.ref.strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref must not be empty")

    async with async_session_factory() as session:
        await pin_workflow_repo_ref(session, ref)
    return await get_workflow_repo_status()


@router.get("/platform/workflow-repo/versions", response_model=list[WorkflowRepoVersionResponse])
async def list_workflow_repo_versions():
    repo_url = settings.workflow_repo_url.strip()
    if not repo_url:
        return []
    return await _fetch_github_tags(repo_url)


@router.get("/platform/memories", response_model=PlatformMemoriesResponse)
async def get_platform_memories():
    (live_bank_ids, _), hindsight_available, agent_memories = await asyncio.gather(
        _list_hindsight_banks_async(),
        _hindsight_available_async(),
        asyncio.to_thread(_agent_memories_catalog),
    )
    return PlatformMemoriesResponse(
        hindsight_available=hindsight_available,
        hindsight_banks=_hindsight_banks_catalog(live_bank_ids),
        agent_memories=agent_memories,
    )


@router.get("/platform/background-jobs", response_model=PlatformBackgroundJobsResponse)
async def get_platform_background_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    async with async_session_factory() as session:
        total = int((await session.execute(select(func.count()).select_from(BackgroundJobRun))).scalar_one())
        result = await session.execute(
            select(BackgroundJobRun).order_by(BackgroundJobRun.started_at.desc()).limit(limit).offset(offset)
        )
        runs = result.scalars().all()

    return PlatformBackgroundJobsResponse(
        items=[_background_job_run_response(run) for run in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/platform/memories/hindsight/{bank_id}", response_model=HindsightBankDetailResponse)
async def get_hindsight_bank_entries(bank_id: str, limit: int = Query(default=12, ge=1, le=50)):
    (
        (live_bank_ids, bank_errors),
        (entries, entry_errors),
        (stats, stats_errors),
        (graph, graph_errors),
    ) = await asyncio.gather(
        _list_hindsight_banks_async(),
        _fetch_hindsight_entries_async(bank_id, limit),
        _fetch_hindsight_stats_async(bank_id),
        _fetch_hindsight_graph_async(bank_id, min(limit * 2, 24)),
    )

    warnings = [
        *bank_errors,
        *entry_errors,
        *stats_errors,
        *graph_errors,
    ]
    listed_in_hindsight = bank_id in live_bank_ids
    if not listed_in_hindsight:
        warnings.append("This bank is not currently returned by Hindsight list_banks on the connected instance.")
    if not entries and stats.total_nodes == 0 and stats.total_documents == 0:
        warnings.append("Hindsight returned no live memories or graph data for this bank.")

    return HindsightBankDetailResponse(
        bank_id=bank_id,
        listed_in_hindsight=listed_in_hindsight,
        warnings=list(dict.fromkeys(warnings)),
        stats=stats,
        graph=graph,
        entries=entries,
    )


@router.get("/platform/memories/agents/{agent_name}", response_model=AgentMemoryDetailResponse)
async def get_agent_memory_detail(
    agent_name: str,
    max_files: int = Query(default=12, ge=1, le=40),
    preview_chars: int = Query(default=1500, ge=300, le=6000),
):
    catalog = {item.agent_name: item for item in _agent_memories_catalog()}
    target = catalog.get(agent_name)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Agent memory for '{agent_name}' not found")
    if not target.latest_key:
        return AgentMemoryDetailResponse(agent_name=agent_name, archive_key=None, files=[])

    try:
        archive_bytes = download_bytes(BUCKET_AGENT_MEMORY, target.latest_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to read memory archive: {exc}") from exc
    if archive_bytes is None:
        raise HTTPException(status_code=404, detail=f"Memory archive '{target.latest_key}' not found")

    try:
        files = _preview_agent_memory_files(
            archive_bytes,
            max_files=max_files,
            max_preview_chars=preview_chars,
        )
    except tarfile.TarError as exc:
        raise HTTPException(status_code=502, detail=f"Invalid memory archive format: {exc}") from exc

    return AgentMemoryDetailResponse(
        agent_name=agent_name,
        archive_key=target.latest_key,
        files=files,
    )


# ─── Agent Secrets Management ────────────────────────────────────────
# Pulumi-style inline encrypted secrets in agent.yaml.
# Gateway encrypts (has age public key), session manager decrypts (has identity).


class SecretItem(BaseModel):
    name: str
    description: str | None = None
    has_value: bool


class SecretUpdate(BaseModel):
    """Plaintext value — gateway encrypts it before writing to agent.yaml."""

    value: str
    description: str | None = None


def _load_agent_yaml(agent_name: str) -> tuple[dict, str]:
    """Load agent.yaml from configured workflow roots. Returns (config, file_path)."""
    package = find_workflow_package(agent_name)
    if package is not None:
        return package.config, str(package.agent_yaml_path)

    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")


def _save_agent_yaml(config: dict, path: str) -> None:
    """Write agent.yaml back to disk preserving order."""
    import yaml

    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


@router.get("/agents/{name}/secrets", response_model=list[SecretItem])
async def list_secrets(name: str):
    """List secrets for an agent (names + descriptions, never values)."""
    from shared.lib.crypto import list_agent_secrets

    config, _ = _load_agent_yaml(name)
    return [
        SecretItem(name=s["name"], description=s.get("description"), has_value=s["has_value"])
        for s in list_agent_secrets(config)
    ]


@router.put("/agents/{name}/secrets/{key}")
async def upsert_secret(name: str, key: str, body: SecretUpdate):
    """Encrypt a plaintext secret and save to agent.yaml."""
    from shared.lib.config import settings
    from shared.lib.crypto import encrypt_secret

    if not settings.age_public_key:
        raise HTTPException(status_code=500, detail="AGE_PUBLIC_KEY not configured")

    config, path = _load_agent_yaml(name)
    encrypted = encrypt_secret(body.value, public_key=settings.age_public_key)

    secrets = config.setdefault("secrets", {})
    entry: dict[str, str] = {"encrypted": encrypted}
    if body.description:
        entry["description"] = body.description
    elif key in secrets and "description" in secrets[key]:
        entry["description"] = secrets[key]["description"]
    secrets[key] = entry

    _save_agent_yaml(config, path)
    return {"status": "ok", "key": key}


@router.delete("/agents/{name}/secrets/{key}")
async def delete_secret(name: str, key: str):
    """Remove a secret from agent.yaml."""
    config, path = _load_agent_yaml(name)
    secrets = config.get("secrets", {})
    if key not in secrets:
        raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")
    del secrets[key]
    if not secrets:
        config.pop("secrets", None)
    _save_agent_yaml(config, path)
    return {"status": "ok", "key": key}
