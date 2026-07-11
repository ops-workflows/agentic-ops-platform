"""Long-term Memory MCP Server (backed by Hindsight) — use for past incident recall and workflow learning, not live system state."""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, get_env
from shared.lib.memory_catalog import BANK_INCIDENT_RCA, BANK_WORKFLOW_LEARNING, WorkflowBankKind, load_workflow_banks

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

HINDSIGHT_URL = get_env("HINDSIGHT_URL", "http://hindsight:8888")
BANKS_API_PREFIX = "/v1/default/banks"

mcp = FastMCP("Long-term Memory MCP Server")


def _hindsight_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
    url = f"{HINDSIGHT_URL}{endpoint}"
    timeout = float(kwargs.pop("timeout", 30.0))
    try:
        with httpx.Client(timeout=timeout) as client:
            if method == "POST":
                response = client.post(url, json=kwargs.get("json"))
            else:
                response = client.get(url, params=kwargs.get("params"))
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.error("Hindsight request failed: %s %s — %s", method, url, exc)
        return {"error": str(exc)}


def _resolve_bank_id(bank_id: str | None, headers: dict[str, str], bank_kind: WorkflowBankKind) -> str:
    explicit = (bank_id or "").strip()
    if explicit:
        return explicit
    workflow = headers.get("x-task-workflow", "").strip().lower()
    workflow_banks = load_workflow_banks()
    if bank_kind == "learning":
        return workflow_banks["learning"].get(workflow, BANK_WORKFLOW_LEARNING)
    return workflow_banks["business"].get(workflow, BANK_INCIDENT_RCA)


def _stringify_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    if not metadata:
        return {}
    return {str(key): str(value) for key, value in metadata.items() if value is not None}


def _query_with_time_focus(query: str, time_range: str | None) -> str:
    if not time_range:
        return query
    return f"{query}\nFocus on evidence from the last {time_range}. Ignore older patterns unless they are still clearly active."


def _query_timestamp_for_range(time_range: str | None, *, now: datetime | None = None) -> str | None:
    if not time_range:
        return None

    match = re.fullmatch(r"\s*(\d+)\s*([hdw])\s*", time_range.strip().lower())
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    delta_map = {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }
    anchor = now or datetime.now(UTC)
    return (anchor - delta_map[unit]).isoformat().replace("+00:00", "Z")


@mcp.tool(annotations={"openWorldHint": False})
def retain_incident(
    content: Annotated[str, "Full incident analysis text to store in long-term memory."],
    metadata: Annotated[
        dict[str, Any] | None, "Optional structured metadata such as service or root cause category."
    ] = None,
    bank_kind: Annotated[
        WorkflowBankKind,
        "Workflow memory kind: business for digest-facing RCA memory, learning for workflow-improvement notes.",
    ] = "business",
    bank_id: Annotated[
        str | None, "Optional memory bank override. Defaults to the workflow-specific bank for the selected bank_kind."
    ] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Store a durable incident analysis or workflow-learning note for future recall."""
    resolved_bank = _resolve_bank_id(bank_id, headers, bank_kind)
    return _hindsight_request(
        "POST",
        f"{BANKS_API_PREFIX}/{resolved_bank}/memories",
        json={
            "async": True,
            "items": [
                {
                    "content": content,
                    "metadata": _stringify_metadata(metadata),
                }
            ],
        },
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def recall_similar(
    query: Annotated[str, "Description of the current issue to search against past incidents."],
    limit: Annotated[int, "Maximum number of similar incidents to return."] = 5,
    bank_kind: Annotated[
        WorkflowBankKind,
        "Workflow memory kind: business for incident recall, learning for workflow-improvement recall.",
    ] = "business",
    bank_id: Annotated[
        str | None, "Optional memory bank override. Defaults to the workflow-specific bank for the selected bank_kind."
    ] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this for similar past incidents when historical precedent may explain the current problem."""
    resolved_bank = _resolve_bank_id(bank_id, headers, bank_kind)
    return _hindsight_request(
        "POST",
        f"{BANKS_API_PREFIX}/{resolved_bank}/memories/recall",
        json={"query": query, "max_tokens": 4096},
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def reflect_patterns(
    query: Annotated[str, "What pattern or class of incidents to synthesize."],
    time_range: Annotated[
        str | None, "Optional recent time window such as 24h or 7d to constrain the synthesis."
    ] = None,
    bank_kind: Annotated[
        WorkflowBankKind,
        "Workflow memory kind: business for incident themes, learning for workflow-improvement themes.",
    ] = "business",
    bank_id: Annotated[
        str | None, "Optional memory bank override. Defaults to the workflow-specific bank for the selected bank_kind."
    ] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this to synthesize recurring patterns across stored incidents instead of pulling example incidents one by one."""
    resolved_bank = _resolve_bank_id(bank_id, headers, bank_kind)
    return _hindsight_request(
        "POST",
        f"{BANKS_API_PREFIX}/{resolved_bank}/reflect",
        json={"query": _query_with_time_focus(query, time_range)},
        timeout=60.0,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def recall_for_digest(
    query: Annotated[str, "Query for recent incidents to summarize in a digest."],
    time_range: Annotated[str, "Time window such as 24h or 7d."] = "24h",
    bank_kind: Annotated[
        WorkflowBankKind,
        "Workflow memory kind. Keep the default business bank for digest generation unless you intentionally want learning notes.",
    ] = "business",
    bank_id: Annotated[
        str | None, "Optional memory bank override. Defaults to the workflow-specific bank for the selected bank_kind."
    ] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this for digest generation when you need recent raw incidents rather than a finished summary."""
    resolved_bank = _resolve_bank_id(bank_id, headers, bank_kind)
    payload = {
        "query": _query_with_time_focus(query, time_range),
        "max_tokens": 4096,
    }
    if query_timestamp := _query_timestamp_for_range(time_range):
        payload["query_timestamp"] = query_timestamp

    return _hindsight_request(
        "POST",
        f"{BANKS_API_PREFIX}/{resolved_bank}/memories/recall",
        json=payload,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-memory"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
