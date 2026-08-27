"""Splunk MCP Server — use for application and service logs, saved searches, and fired-alert history."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, extract_bearer_token, require_header, validate_base_url
from mcps.evidence import (
    compile_redaction_patterns,
    fit_evidence_budget,
    redact_text,
)
from shared.lib.platform_secrets import load_mcp_server_config

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

mcp = FastMCP("Splunk MCP Server")

MAX_EVIDENCE_GROUPS = 20
MAX_SAMPLES_PER_GROUP = 2
MAX_STACK_FRAMES = 12
MAX_STACK_CHARS = 4_096
MAX_RESPONSE_BYTES = 2_000_000
_CONFIG_PATH = os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml")
_POLICY = load_mcp_server_config(_CONFIG_PATH, "splunk")
_EVIDENCE_POLICY = load_mcp_server_config(_CONFIG_PATH, "evidence")
_REDACTION_PATTERNS = compile_redaction_patterns(_EVIDENCE_POLICY.get("redaction_patterns"))
ALLOWED_HOSTS = frozenset(str(value).lower() for value in _POLICY.get("allowed_hosts", []) if str(value).strip())
ALLOWED_INDEXES = frozenset(str(value) for value in _POLICY.get("allowed_indexes", []) if str(value).strip())
SAVED_SEARCHES = {
    str(alias): str(identity)
    for alias, identity in (_POLICY.get("saved_searches") or {}).items()
    if str(alias).strip() and str(identity).strip()
}
CA_BUNDLE = str(_POLICY.get("ca_bundle") or "").strip()
MAX_QUERY_RESULTS = min(int(_POLICY.get("max_results") or 500), 500)
MAX_QUERY_CHARS = min(int(_POLICY.get("max_query_chars") or 4_096), 8_192)
MAX_WINDOW_SECONDS = min(int(_POLICY.get("max_window_hours") or 24), 168) * 3_600
MAX_EVIDENCE_BYTES = max(8_192, min(int(_POLICY.get("max_evidence_bytes") or 65_536), 262_144))
_CHECKPOINT_RE = re.compile(
    r"(?P<operation>[\w.:-]+)__checkpoint\s*=>\s*HTTP\s+"
    r"(?P<method>[A-Z]+)\s+[\"'](?P<path>[^\"']+)",
    re.IGNORECASE,
)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
_INDEX_RE = re.compile(r"(?i)(?:^|\s)index\s*=\s*[\"']?([a-z0-9_.:-]+)")
_UNSAFE_SPL_RE = re.compile(r"(?i)(?:^|[|\s])(collect|delete|dump|outputcsv|outputlookup|sendemail)\b")
_EMBEDDED_TIME_RE = re.compile(r"(?i)(?:^|\s)(?:earliest|latest)\s*=")
_RELATIVE_TIME_RE = re.compile(r"^-(\d+)([mhd])$")
_SID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _json_object(value: Any, warnings: list[str], field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip().startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        warnings.append(f"invalid_{field}_json")
        return {}
    if not isinstance(parsed, dict):
        warnings.append(f"non_object_{field}_json")
        return {}
    return parsed


def _redact(value: Any) -> str:
    return redact_text(value, _REDACTION_PATTERNS)


def _path_template(path: str) -> str:
    path = path.split("?", 1)[0]
    path = _UUID_RE.sub("{id}", path)
    return re.sub(r"/(?=\d+(?:/|$))\d+", "/{id}", path)


def _stack_excerpt(message: str) -> tuple[str, bool]:
    lines = message.splitlines()
    selected = lines[:MAX_STACK_FRAMES]
    excerpt = "\n".join(selected)
    truncated = len(lines) > MAX_STACK_FRAMES or len(excerpt) > MAX_STACK_CHARS
    return _redact(excerpt[:MAX_STACK_CHARS]), truncated


def _normalized_row(row: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    raw = _json_object(row.get("_raw"), warnings, "raw")
    values = {**raw, **{key: value for key, value in row.items() if key != "_raw"}}
    exception = _json_object(values.get("exception"), warnings, "exception")
    message = str(values.get("message") or exception.get("detailMessage") or "")
    checkpoint = _CHECKPOINT_RE.search(message)
    endpoint = _path_template(checkpoint.group("path")) if checkpoint else ""
    detail = str(exception.get("detailMessage") or exception.get("message") or "")
    stack_excerpt, stack_truncated = _stack_excerpt(message)
    return {
        "timestamp": str(values.get("_time") or values.get("timestamp") or ""),
        "source": _redact(values.get("source")),
        "severity": _redact(values.get("severity") or values.get("level")),
        "exception_class": _redact(
            exception.get("exceptionClass") or exception.get("class") or values.get("exception_class")
        ),
        "detail": _redact(detail),
        "operation": checkpoint.group("operation") if checkpoint else "",
        "method": checkpoint.group("method").upper() if checkpoint else "",
        "endpoint": endpoint,
        "stack_excerpt": stack_excerpt,
        "stack_truncated": stack_truncated,
    }


def compact_splunk_evidence(
    payload: dict[str, Any],
    *,
    query: str,
    earliest: str,
    latest: str,
    max_samples_per_group: int = MAX_SAMPLES_PER_GROUP,
) -> dict[str, Any]:
    """Convert Splunk rows into bounded, redacted occurrence groups."""
    rows = payload.get("results")
    rows = rows if isinstance(rows, list) else []
    warnings: list[str] = []
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            warnings.append("non_object_result")
            continue
        row = _normalized_row(raw_row, warnings)
        group_key = (
            row["source"],
            row["exception_class"],
            row["detail"],
            row["operation"],
            row["method"],
            row["endpoint"],
        )
        grouped[group_key].append(row)

    groups = []
    for members in grouped.values():
        ordered = sorted(members, key=lambda item: item["timestamp"])
        groups.append(
            {
                "count": len(members),
                "first_seen": ordered[0]["timestamp"],
                "last_seen": ordered[-1]["timestamp"],
                "samples": ordered[: max(1, min(max_samples_per_group, 5))],
            }
        )
    groups.sort(key=lambda group: (-group["count"], group["first_seen"], group["last_seen"]))
    groups_truncated = len(groups) > MAX_EVIDENCE_GROUPS
    groups = groups[:MAX_EVIDENCE_GROUPS]
    bundle = {
        "schema_version": 1,
        "provider": "splunk",
        "query": {"spl": query, "earliest": earliest, "latest": latest},
        "total_results": int(payload.get("count", len(rows))),
        "processed_results": len(rows),
        "groups": groups,
        "warnings": sorted(set(warnings)),
        "truncated": groups_truncated
        or any(sample["stack_truncated"] for group in groups for sample in group["samples"]),
    }
    return fit_evidence_budget(bundle, MAX_EVIDENCE_BYTES)


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
    token = request_headers.get("x-splunk-token", "").strip() or extract_bearer_token(request_headers)
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

    raise ValueError("Provide a Splunk token or username/password through request headers")


def _validate_splunk_query(query: str) -> str | None:
    if len(query) > MAX_QUERY_CHARS:
        return f"Splunk query exceeds the {MAX_QUERY_CHARS}-character limit"
    if _UNSAFE_SPL_RE.search(query):
        return "Splunk query contains a disallowed command"
    if _EMBEDDED_TIME_RE.search(query):
        return "Splunk query must use the tool's earliest and latest parameters"
    indexes = set(_INDEX_RE.findall(query))
    if not indexes:
        return "Splunk query must include an explicit index"
    if not ALLOWED_INDEXES:
        return "Splunk allowed_indexes policy is not configured"
    denied = sorted(indexes - ALLOWED_INDEXES)
    if denied:
        return f"Splunk indexes are not allowed: {', '.join(denied)}"
    return None


def _parse_time(value: str, now: datetime) -> datetime | None:
    if value == "now":
        return now
    relative = _RELATIVE_TIME_RE.fullmatch(value)
    if relative:
        amount = int(relative.group(1))
        seconds = amount * {"m": 60, "h": 3_600, "d": 86_400}[relative.group(2)]
        return now - timedelta(seconds=seconds)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _validate_time_range(earliest: str, latest: str) -> str | None:
    now = datetime.now(UTC)
    start = _parse_time(earliest, now)
    end = _parse_time(latest, now)
    if start is None or end is None:
        return "Splunk earliest/latest must be relative (-15m, -1h, -1d), now, or timezone-aware ISO 8601"
    if start >= end:
        return "Splunk earliest must be before latest"
    if end > now + timedelta(minutes=5):
        return "Splunk latest cannot be in the future"
    if (end - start).total_seconds() > MAX_WINDOW_SECONDS:
        return f"Splunk time window exceeds the {MAX_WINDOW_SECONDS // 3_600}-hour limit"
    return None


def _validate_sid(sid: str) -> str | None:
    if not _SID_RE.fullmatch(sid):
        return "Splunk search job sid has invalid syntax"
    return None


def _parse_alert_url(alert_url: str) -> dict[str, Any]:
    parsed = urlparse(alert_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"error": "Splunk alert URL must use https and include a host"}
    if not ALLOWED_HOSTS or parsed.hostname.lower() not in ALLOWED_HOSTS:
        return {"error": "Splunk alert URL host is not allowed by platform policy"}
    sid_values = parse_qs(parsed.query).get("sid", [])
    if len(sid_values) != 1 or _validate_sid(sid_values[0]):
        return {"error": "Splunk alert URL must contain one valid sid parameter"}
    path_parts = [part for part in parsed.path.split("/") if part]
    app_index = path_parts.index("app") + 1 if "app" in path_parts else len(path_parts)
    app = path_parts[app_index] if app_index < len(path_parts) else ""
    return {
        "provider": "splunk",
        "host": parsed.hostname.lower(),
        "app": app,
        "sid": sid_values[0],
        "url": alert_url,
    }


def _project_job_metadata(payload: dict[str, Any], sid: str) -> dict[str, Any]:
    entries = payload.get("entry")
    entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
    dispatch_state = str(content.get("dispatchState") or "UNKNOWN").upper()
    is_done = bool(content.get("isDone")) or dispatch_state == "DONE"
    return {
        "provider": "splunk",
        "sid": sid,
        "dispatch_state": dispatch_state,
        "is_done": is_done,
        "is_failed": bool(content.get("isFailed")) or dispatch_state == "FAILED",
        "event_count": int(content.get("eventCount") or 0),
        "result_count": int(content.get("resultCount") or 0),
        "run_duration": float(content.get("runDuration") or 0),
        "earliest_time": str(content.get("earliestTime") or ""),
        "latest_time": str(content.get("latestTime") or ""),
        "search": str(content.get("search") or ""),
    }


def _saved_search_identity(owner: str, app: str, name: str) -> str | None:
    if not all(_NAMESPACE_RE.fullmatch(value) for value in (owner, app, name)):
        return None
    return f"{owner}/{app}/{name}"


def _tls_verify() -> bool | str:
    if not CA_BUNDLE:
        return True
    bundle = Path(CA_BUNDLE)
    if not bundle.is_file():
        raise ValueError(f"Configured Splunk CA bundle is not mounted: {CA_BUNDLE}")
    if bundle.stat().st_size == 0:
        raise ValueError(f"Configured Splunk CA bundle is empty: {CA_BUNDLE}")
    return str(bundle)


def _project_saved_search(payload: dict[str, Any], owner: str, app: str, name: str) -> dict[str, Any]:
    entries = payload.get("entry")
    entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
    return {
        "provider": "splunk",
        "identity": {"owner": owner, "app": app, "name": name},
        "description": str(content.get("description") or ""),
        "search": str(content.get("search") or ""),
        "disabled": bool(content.get("disabled")),
        "cron_schedule": str(content.get("cron_schedule") or ""),
        "dispatch_earliest_time": str(content.get("dispatch.earliest_time") or ""),
        "dispatch_latest_time": str(content.get("dispatch.latest_time") or ""),
        "alert_type": str(content.get("alert_type") or ""),
        "alert_comparator": str(content.get("alert_comparator") or ""),
        "alert_threshold": str(content.get("alert_threshold") or ""),
        "actions": str(content.get("actions") or ""),
    }


def _splunk_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
    request_headers = kwargs.pop("headers")
    try:
        base_url = validate_base_url(
            require_header(headers=request_headers, header_name="x-splunk-base-url", description="Splunk base URL"),
            header_name="x-splunk-base-url",
        )
        hostname = (urlparse(base_url).hostname or "").lower()
        if not ALLOWED_HOSTS or hostname not in ALLOWED_HOSTS:
            return {"error": "Splunk base URL host is not allowed by platform policy"}
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{base_url}{endpoint}"
    try:
        verify = _tls_verify()
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
    try:
        with httpx.Client(timeout=60.0, verify=verify) as client:
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
            if len(response.content) > MAX_RESPONSE_BYTES:
                return {"error": f"Splunk response exceeds the {MAX_RESPONSE_BYTES}-byte limit"}
            return _parse_response(response.text)
    except httpx.HTTPStatusError as exc:
        logger.error("Splunk request failed: %s %s — %s", method, url, exc)
        return {"error": str(exc), "status_code": exc.response.status_code}
    except httpx.HTTPError as exc:
        logger.error("Splunk request failed: %s %s — %s", method, url, exc)
        return {"error": str(exc)}
    except OSError as exc:
        logger.error("Splunk TLS setup failed: %s", exc)
        return {"error": f"Splunk TLS setup failed: {exc}"}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def search_logs(
    query: Annotated[
        str,
        "One bounded SPL query with an explicit allowed index and a provider-derived request/session/signature pivot; "
        "do not invent terms from the alert title.",
    ],
    earliest: Annotated[
        str | None,
        "Earliest bound derived from the finalized alert job or exact correlation interval, such as -15m or an ISO "
        "timestamp.",
    ] = None,
    latest: Annotated[
        str | None,
        "Latest bound derived from the finalized alert job or exact correlation interval, such as now or an ISO "
        "timestamp.",
    ] = None,
    max_results: Annotated[int, "Maximum number of results to return."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Run at most one justified follow-up SPL query after exact finalized alert results supply a stable pivot."""
    search_query = query if query.startswith("search") else f"search {query}"
    validation_error = _validate_splunk_query(search_query)
    if validation_error:
        return {"error": validation_error}
    effective_earliest = earliest or "-1h"
    effective_latest = latest or "now"
    time_error = _validate_time_range(effective_earliest, effective_latest)
    if time_error:
        return {"error": time_error}
    max_results = max(1, min(max_results, MAX_QUERY_RESULTS))
    params: dict[str, Any] = {
        "search": search_query,
        "exec_mode": "oneshot",
        "output_mode": "json",
        "count": max_results,
    }
    params["earliest_time"] = effective_earliest
    params["latest_time"] = effective_latest
    response = _splunk_request(
        "GET",
        "/services/search/jobs/export",
        headers=headers,
        params=params,
    )
    if "error" in response:
        return response
    return compact_splunk_evidence(
        response,
        query=search_query,
        earliest=effective_earliest,
        latest=effective_latest,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def parse_alert_url(
    alert_url: Annotated[str, "Full Splunk alert/search URL containing a sid query parameter."],
) -> dict[str, Any]:
    """Validate an alert URL against platform policy and extract its dispatch ID without network access."""
    return _parse_alert_url(alert_url)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_search_job(
    sid: Annotated[str, "Splunk search job dispatch ID."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Retrieve this exact alert dispatch first, without reconstructing the search."""
    validation_error = _validate_sid(sid)
    if validation_error:
        return {"error": validation_error}
    response = _splunk_request(
        "GET",
        f"/services/search/jobs/{quote(sid, safe='')}",
        headers=headers,
        params={"output_mode": "json"},
    )
    if "error" in response:
        return response
    return _project_job_metadata(response, sid)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_search_results(
    sid: Annotated[str, "Splunk search job dispatch ID."],
    max_results: Annotated[int, "Maximum finalized results to retrieve and compact."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return compact evidence only from this exact finalized alert job; stop on unavailable or non-final jobs."""
    job = get_search_job(sid, headers=headers)
    if "error" in job:
        return job
    if not job["is_done"] or job["is_failed"]:
        return {"error": "Splunk search job is not successfully finalized", "job": job}
    max_results = max(1, min(max_results, MAX_QUERY_RESULTS))
    response = _splunk_request(
        "GET",
        f"/services/search/jobs/{quote(sid, safe='')}/results",
        headers=headers,
        params={"output_mode": "json", "count": max_results, "offset": 0},
    )
    if "error" in response:
        return response
    evidence = compact_splunk_evidence(
        response,
        query=str(job["search"]),
        earliest=str(job["earliest_time"]),
        latest=str(job["latest_time"]),
    )
    evidence["job"] = {key: value for key, value in job.items() if key != "search"}
    evidence["finalized"] = True
    return evidence


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def run_saved_search(
    name: Annotated[str, "Platform-configured saved-search name."],
    earliest: Annotated[str, "Bounded earliest time, such as -1h or an ISO timestamp."] = "-1h",
    latest: Annotated[str, "Bounded latest time, such as now or an ISO timestamp."] = "now",
    max_results: Annotated[int, "Maximum results to group and summarize."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Run one configured saved search over a bounded window and return compact occurrence statistics."""
    identity = SAVED_SEARCHES.get(name)
    if identity is None:
        return {"error": "Splunk saved search is not configured by platform policy"}
    try:
        owner, app, saved_name = identity.split("/", 2)
    except ValueError:
        return {"error": "Configured Splunk saved-search identity must use owner/app/name syntax"}
    if _saved_search_identity(owner, app, saved_name) is None:
        return {"error": "Configured Splunk saved-search identity has invalid syntax"}
    time_error = _validate_time_range(earliest, latest)
    if time_error:
        return {"error": time_error}

    definition_response = _splunk_request(
        "GET",
        f"/servicesNS/{quote(owner, safe='')}/{quote(app, safe='')}/saved/searches/{quote(saved_name, safe='')}",
        headers=headers,
        params={"output_mode": "json"},
    )
    if "error" in definition_response:
        return definition_response
    definition = _project_saved_search(definition_response, owner, app, saved_name)
    search_query = str(definition.get("search") or "")
    search_query = search_query if search_query.startswith("search") else f"search {search_query}"
    query_error = _validate_splunk_query(search_query)
    if query_error:
        return {"error": query_error}

    response = _splunk_request(
        "GET",
        "/services/search/jobs/export",
        headers=headers,
        params={
            "search": search_query,
            "exec_mode": "oneshot",
            "output_mode": "json",
            "count": max(1, min(max_results, MAX_QUERY_RESULTS)),
            "earliest_time": earliest,
            "latest_time": latest,
        },
    )
    if "error" in response:
        return response
    evidence = compact_splunk_evidence(
        response,
        query=search_query,
        earliest=earliest,
        latest=latest,
    )
    evidence["saved_search"] = {"alias": name, **definition["identity"]}
    return fit_evidence_budget(evidence, MAX_EVIDENCE_BYTES)


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
