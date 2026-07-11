"""Splunk MCP Server — use for application and service logs, saved searches, and fired-alert history."""

from __future__ import annotations

import json
import logging
import sys
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import require_header, validate_base_url

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

mcp = FastMCP("Splunk MCP Server")


def _parse_response(text: str) -> dict[str, Any]:
    """Parse a splunkd REST response, handling both JSON and NDJSON.

    ``jobs/export`` streams results as newline-delimited JSON: each line is a
    separate object like ``{"result": {...}}`` or a trailing footer. Collect all
    ``result`` values into a unified ``{"results": [...], "count": N}`` dict.
    Single-object responses (e.g. auth/login) are returned as-is.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    if len(lines) == 1:
        return json.loads(lines[0])
    results = []
    for line in lines:
        obj = json.loads(line)
        if "result" in obj:
            results.append(obj["result"])
    return {"results": results, "count": len(results)}


def _build_auth_headers(client: httpx.Client, base_url: str, request_headers: dict[str, str]) -> dict[str, str]:
    """Return the HTTP headers needed to authenticate a splunkd REST request.

    - ``x-splunk-token`` present  →  ``{"Authorization": "Bearer <token>"``}
      Works against splunkd directly on port 8089 or any proxy that accepts
      a bearer JWT.

    - ``x-splunk-username`` + ``x-splunk-password`` present  →  login via
      ``/services/auth/login``, then ``{"Cookie": "splunkd_8000=<session_key>"``}
      Works against the Splunk Web ``/__raw/`` proxy on 443 where the management
      port is not directly reachable.
    """
    token = request_headers.get("x-splunk-token", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    username = request_headers.get("x-splunk-username", "").strip()
    password = request_headers.get("x-splunk-password", "")
    if username and password:
        resp = client.post(
            f"{base_url}/services/auth/login",
            data={"username": username, "password": password, "output_mode": "json"},
        )
        resp.raise_for_status()
        session_key = resp.json()["sessionKey"]
        return {"Cookie": f"splunkd_8000={session_key}"}

    raise ValueError("Provide x-splunk-token, or x-splunk-username and x-splunk-password")


def _splunk_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
    request_headers = kwargs.pop("headers")
    try:
        base_url = validate_base_url(
            require_header(headers=request_headers, header_name="x-splunk-base-url", description="Splunk base URL"),
            header_name="x-splunk-base-url",
        )
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{base_url}{endpoint}"
    try:
        with httpx.Client(timeout=60.0, verify=False) as client:  # noqa: S501
            try:
                auth_headers = _build_auth_headers(client, base_url, request_headers)
            except ValueError as exc:
                return {"error": str(exc)}
            client.headers.update(auth_headers)
            if method == "POST":
                response = client.post(url, data=kwargs.get("data", {}))
            else:
                response = client.get(url, params=kwargs.get("params", {}))
            response.raise_for_status()
            return _parse_response(response.text)
    except httpx.HTTPError as exc:
        logger.error("Splunk request failed: %s %s — %s", method, url, exc)
        return {"error": str(exc)}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def search_logs(
    query: Annotated[str, "Splunk SPL query string."],
    earliest: Annotated[str | None, "Optional earliest time, such as -1h or an ISO timestamp."] = None,
    latest: Annotated[str | None, "Optional latest time, such as now or an ISO timestamp."] = None,
    max_results: Annotated[int, "Maximum number of results to return."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this for bounded SPL queries when you need current application or service log evidence."""
    search_query = query if query.startswith("search") else f"search {query}"
    params: dict[str, Any] = {
        "search": search_query,
        "exec_mode": "oneshot",
        "output_mode": "json",
        "count": max_results,
    }
    if earliest:
        params["earliest_time"] = earliest
    if latest:
        params["latest_time"] = latest
    return _splunk_request(
        "GET",
        "/services/search/jobs/export",
        headers=headers,
        params=params,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_saved_search(
    name: Annotated[str, "Name of the saved search to inspect."],
    earliest: Annotated[str | None, "Optional earliest time override."] = None,
    latest: Annotated[str | None, "Optional latest time override."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this when investigation depends on the state or output of a known saved search."""
    endpoint = f"/servicesNS/-/-/saved/searches/{quote(name, safe='')}/history"
    params: dict[str, Any] = {"output_mode": "json", "count": 1}
    if earliest:
        params["earliest_time"] = earliest
    if latest:
        params["latest_time"] = latest
    return _splunk_request(
        "GET",
        endpoint,
        headers=headers,
        params=params,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_alert_events(
    alert_name: Annotated[str, "Alert name to look up in Splunk fired alerts."],
    count: Annotated[int, "Maximum number of recent alert events to return."] = 10,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this when you need recent fired-alert entries for a known Splunk alert name."""
    endpoint = f"/servicesNS/-/-/alerts/fired_alerts/{quote(alert_name, safe='')}"
    return _splunk_request(
        "GET",
        endpoint,
        headers=headers,
        params={"output_mode": "json", "count": count},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-splunk"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
