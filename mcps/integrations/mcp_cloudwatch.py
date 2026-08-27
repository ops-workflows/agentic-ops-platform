"""CloudWatch Logs MCP Server — use for AWS infrastructure logs and known CloudWatch log groups when Splunk is not the right source."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Annotated, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env
from mcps.evidence import (
    compile_redaction_patterns,
    fit_evidence_budget,
    redact_text,
)
from shared.lib.platform_secrets import load_mcp_server_config

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

REGION_PATTERN = re.compile(r"^[a-z0-9-]+$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
ALARM_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):cloudwatch:(?P<region>[a-z0-9-]+):"
    r"(?P<account>\d{12}):alarm:(?P<name>[^\r\n]{1,255})$"
)
_CONFIG_PATH = os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml")
_POLICY = load_mcp_server_config(_CONFIG_PATH, "cloudwatch")
_EVIDENCE_POLICY = load_mcp_server_config(_CONFIG_PATH, "evidence")
_REDACTION_PATTERNS = compile_redaction_patterns(_EVIDENCE_POLICY.get("redaction_patterns"))
ALLOWED_REGIONS = frozenset(str(value) for value in _POLICY.get("allowed_regions", []) if str(value).strip())
ALLOWED_ACCOUNT_IDS = frozenset(str(value) for value in _POLICY.get("allowed_account_ids", []) if str(value).strip())
ALLOWED_LOG_GROUP_PREFIXES = tuple(
    str(value) for value in _POLICY.get("allowed_log_group_prefixes", []) if str(value).strip()
)
ALLOWED_LOG_GROUPS = frozenset(str(value) for value in _POLICY.get("allowed_log_groups", []) if str(value).strip())
ALARM_LOG_GROUP_MAPPINGS = {
    str(alarm_name): tuple(str(value) for value in log_groups if str(value).strip())
    for alarm_name, log_groups in _POLICY.get("alarm_log_group_mappings", {}).items()
    if isinstance(log_groups, list)
}
ALARM_DIMENSION_LOG_GROUP_TEMPLATES = {
    str(dimension_name): tuple(str(value) for value in templates if "{value}" in str(value))
    for dimension_name, templates in _POLICY.get("alarm_dimension_log_group_templates", {}).items()
    if isinstance(templates, list)
}
MAX_QUERY_RESULTS = min(int(_POLICY.get("max_results") or 500), 500)
MAX_QUERY_CHARS = min(int(_POLICY.get("max_query_chars") or 4_096), 8_192)
MAX_WINDOW_SECONDS = min(int(_POLICY.get("max_window_hours") or 24), 168) * 3_600
MAX_SCAN_BYTES = max(1, min(int(_POLICY.get("max_scan_bytes") or 536_870_912), 10_737_418_240))
MAX_EVIDENCE_BYTES = max(8_192, min(int(_POLICY.get("max_evidence_bytes") or 65_536), 262_144))
MAX_EVIDENCE_GROUPS = 20
MAX_SAMPLES_PER_GROUP = 2
MAX_STACK_FRAMES = 12
MAX_STACK_CHARS = 4_096
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_.]*(?:Exception|Error))\b")
_HTTP_RE = re.compile(r"\b(?P<method>GET|POST|PUT|PATCH|DELETE)\s+[\"']?(?P<path>/[^\s\"']+)", re.I)

mcp = FastMCP("CloudWatch Logs MCP Server")


def _get_aws_session(headers: dict[str, str]):
    region = headers.get("x-aws-region", "").strip()
    if not region:
        raise ValueError("AWS region must be provided via the x-aws-region header")
    if not REGION_PATTERN.match(region):
        raise ValueError("x-aws-region must contain only lowercase letters, digits, and hyphens")
    if not ALLOWED_REGIONS or region not in ALLOWED_REGIONS:
        raise ValueError("AWS region is not allowed by platform policy")

    access_key_id = headers.get("x-aws-access-key-id", "").strip()
    secret_access_key = headers.get("x-aws-secret-access-key", "")
    session_token = headers.get("x-aws-session-token", "").strip()
    if not access_key_id or not secret_access_key:
        raise ValueError("AWS credentials must be provided via x-aws-access-key-id and x-aws-secret-access-key headers")

    session = boto3.session.Session(
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token or None,
        region_name=region,
    )
    expected_account = headers.get("x-aws-account-id", "").strip()
    if not ACCOUNT_PATTERN.match(expected_account):
        raise ValueError("x-aws-account-id must be a 12-digit AWS account ID")
    if not ALLOWED_ACCOUNT_IDS or expected_account not in ALLOWED_ACCOUNT_IDS:
        raise ValueError("AWS account is not allowed by platform policy")
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != expected_account:
        raise ValueError(
            f"AWS caller identity account {actual_account} does not match x-aws-account-id {expected_account}"
        )

    return session


def _get_logs_client(headers: dict[str, str]):
    return _get_aws_session(headers).client("logs")


def _validate_alarm_arn(alarm_arn: str, headers: dict[str, str]) -> re.Match[str]:
    match = ALARM_ARN_PATTERN.fullmatch(alarm_arn)
    if match is None:
        raise ValueError("CloudWatch alarm ARN has invalid syntax")
    if match.group("region") != headers.get("x-aws-region", "").strip():
        raise ValueError("CloudWatch alarm ARN region does not match x-aws-region")
    if match.group("account") != headers.get("x-aws-account-id", "").strip():
        raise ValueError("CloudWatch alarm ARN account does not match x-aws-account-id")
    if match.group("region") not in ALLOWED_REGIONS:
        raise ValueError("CloudWatch alarm ARN region is not allowed by platform policy")
    if match.group("account") not in ALLOWED_ACCOUNT_IDS:
        raise ValueError("CloudWatch alarm ARN account is not allowed by platform policy")
    return match


def _get_alarm_client(alarm_arn: str, headers: dict[str, str]):
    match = _validate_alarm_arn(alarm_arn, headers)
    return _get_aws_session(headers).client("cloudwatch"), match.group("name")


def _validate_log_groups(log_group_names: list[str]) -> str | None:
    if not log_group_names:
        return "At least one CloudWatch log group is required"
    if not ALLOWED_LOG_GROUPS and not ALLOWED_LOG_GROUP_PREFIXES:
        return "CloudWatch log-group policy is not configured"
    denied = [
        name
        for name in log_group_names
        if name not in ALLOWED_LOG_GROUPS and not any(name.startswith(prefix) for prefix in ALLOWED_LOG_GROUP_PREFIXES)
    ]
    if denied:
        return f"CloudWatch log groups are not allowed: {', '.join(denied)}"
    return None


def _resolve_dimension_log_groups(alarm_arn: str, headers: dict[str, str]) -> tuple[list[str], str | None]:
    try:
        client, alarm_name = _get_alarm_client(alarm_arn, headers)
        response = client.describe_alarms(AlarmNames=[alarm_name], MaxRecords=1)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return [], str(exc)

    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return [], "CloudWatch alarm was not found"

    dimensions = alarms[0].get("Dimensions", [])
    resolved: list[str] = []
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        name = str(dimension.get("Name") or "")
        value = str(dimension.get("Value") or "")
        if not value:
            continue
        for template in ALARM_DIMENSION_LOG_GROUP_TEMPLATES.get(name, ()):
            candidate = template.replace("{value}", value)
            if candidate not in resolved:
                resolved.append(candidate)

    if not resolved:
        return [], "CloudWatch alarm dimensions have no configured log-group template"
    group_error = _validate_log_groups(resolved)
    if group_error:
        return [], group_error
    return resolved, None


def _parse_relative_time(time_str: str | None) -> int | None:
    if not time_str:
        return None
    if time_str == "now":
        return int(time.time() * 1000)

    candidate = time_str.strip()
    if candidate.startswith("-"):
        unit_map = {"m": 60, "h": 3600, "d": 86400}
        suffix = candidate[-1].lower()
        if suffix in unit_map:
            try:
                amount = int(candidate[1:-1])
                return int((time.time() - amount * unit_map[suffix]) * 1000)
            except ValueError:
                return None

    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def _validate_time_range(start_time: str, end_time: str) -> tuple[int, int] | str:
    start_ms = _parse_relative_time(start_time)
    end_ms = _parse_relative_time(end_time)
    if start_ms is None or end_ms is None:
        return "CloudWatch times must be relative (-15m, -1h, -1d), now, or ISO 8601"
    if start_ms >= end_ms:
        return "CloudWatch start time must be before end time"
    if end_ms > int(time.time() * 1000) + 300_000:
        return "CloudWatch end time cannot be in the future"
    if end_ms - start_ms > MAX_WINDOW_SECONDS * 1000:
        return f"CloudWatch time window exceeds the {MAX_WINDOW_SECONDS // 3_600}-hour limit"
    return start_ms, end_ms


def _redact(value: Any) -> str:
    return redact_text(value, _REDACTION_PATTERNS)


def _path_template(path: str) -> str:
    path = path.split("?", 1)[0]
    path = _UUID_RE.sub("{id}", path)
    return re.sub(r"/(?=\d+(?:/|$))\d+", "/{id}", path)


def _normalize_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = str(row.get("@timestamp") or row.get("timestamp") or "")
    message = str(row.get("@message") or row.get("message") or row.get("error") or "")
    lines = message.splitlines()
    excerpt = "\n".join(lines[:MAX_STACK_FRAMES])[:MAX_STACK_CHARS]
    truncated = len(lines) > MAX_STACK_FRAMES or len("\n".join(lines[:MAX_STACK_FRAMES])) > MAX_STACK_CHARS
    exception = _EXCEPTION_RE.search(message)
    http = _HTTP_RE.search(message)
    endpoint = _path_template(http.group("path")) if http else _redact(row.get("path"))
    safe_excerpt = _redact(excerpt)
    if http:
        safe_excerpt = safe_excerpt.replace(http.group("path"), endpoint)
    return {
        "timestamp": timestamp,
        "log_group": _redact(row.get("@log") or row.get("log_group")),
        "log_stream": _redact(row.get("@logStream") or row.get("log_stream")),
        "severity": _redact(row.get("severity") or row.get("level")),
        "exception_class": exception.group(1) if exception else _redact(row.get("exception_class")),
        "method": http.group("method").upper() if http else _redact(row.get("method")),
        "endpoint": endpoint,
        "message_excerpt": safe_excerpt,
        "stack_truncated": truncated,
    }


def compact_cloudwatch_evidence(
    rows: list[dict[str, Any]],
    *,
    query: str,
    start_time: str,
    end_time: str,
    request_id: str = "",
    statistics: dict[str, Any] | None = None,
    max_samples_per_group: int = MAX_SAMPLES_PER_GROUP,
) -> dict[str, Any]:
    """Convert CloudWatch records into bounded, redacted occurrence groups."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for raw_row in rows:
        row = _normalize_evidence_row(raw_row)
        canonical_message = _UUID_RE.sub("<uuid>", row["message_excerpt"])
        canonical_message = _HTTP_RE.sub(
            lambda match: f"{match.group('method').upper()} {_path_template(match.group('path'))}",
            canonical_message,
        )
        canonical_message = re.sub(r"\b\d{6,}\b", "<id>", canonical_message)
        group_key = (
            row["log_group"],
            row["exception_class"],
            row["method"],
            row["endpoint"],
            canonical_message,
        )
        grouped.setdefault(group_key, []).append(row)

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
        "provider": "cloudwatch",
        "request": {
            "query_id": request_id,
            "query": query,
            "start_time": start_time,
            "end_time": end_time,
        },
        "total_results": len(rows),
        "groups": groups,
        "statistics": statistics or {},
        "warnings": [],
        "truncated": groups_truncated
        or any(sample["stack_truncated"] for group in groups for sample in group["samples"]),
    }
    return fit_evidence_budget(bundle, MAX_EVIDENCE_BYTES)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def search_logs(
    query: Annotated[
        str,
        "CloudWatch Logs Insights query using verified structured failure fields or one provider-derived signature. Do not regex generic JSON field names such as error across the whole message.",
    ],
    log_group_names: Annotated[
        list[str],
        "Exact allowlisted groups returned by get_alarm_log_groups; never infer names from alert text.",
    ],
    start_time: Annotated[
        str,
        "Start time derived from the relevant alarm transition and evaluation horizon, as ISO 8601 or bounded relative time.",
    ] = "-1h",
    end_time: Annotated[str, "End time as ISO 8601 or 'now'."] = "now",
    limit: Annotated[int, "Maximum number of results to return."] = 100,
    samples_per_group: Annotated[int, "Redacted samples retained per occurrence group, maximum 5."] = 2,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Run one bounded Logs Insights query after alarm history and mapped log groups establish its scope."""
    group_error = _validate_log_groups(log_group_names)
    if group_error:
        return {"error": group_error, "results": []}
    if len(query) > MAX_QUERY_CHARS:
        return {"error": f"CloudWatch query exceeds the {MAX_QUERY_CHARS}-character limit"}
    if re.search(r"(?i)(?:^|\|)\s*unmask\b", query):
        return {"error": "CloudWatch query contains the disallowed unmask command"}
    time_range = _validate_time_range(start_time, end_time)
    if isinstance(time_range, str):
        return {"error": time_range}
    try:
        client = _get_logs_client(headers)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "results": []}
    safe_limit = max(1, min(limit, MAX_QUERY_RESULTS))
    start_ms, end_ms = time_range

    try:
        started = client.start_query(
            logGroupNames=log_group_names,
            startTime=start_ms // 1000,
            endTime=end_ms // 1000,
            queryString=query,
            limit=safe_limit,
        )
        query_id = started["queryId"]

        status = "Scheduled"
        result: dict[str, Any] = {}
        for _ in range(60):
            result = client.get_query_results(queryId=query_id)
            status = result["status"]
            if status in {"Complete", "Failed", "Cancelled", "Timeout"}:
                break
            time.sleep(0.5)

        if status != "Complete":
            return {"error": f"Query ended with status: {status}", "results": []}

        stats = result.get("statistics")
        if not isinstance(stats, dict) or not isinstance(stats.get("bytesScanned"), (int, float)):
            return {"error": "CloudWatch query did not report required scan statistics", "results": []}
        if stats["bytesScanned"] > MAX_SCAN_BYTES:
            return {
                "error": f"CloudWatch query scanned more than the {MAX_SCAN_BYTES}-byte limit",
                "results": [],
            }

        results: list[dict[str, Any]] = []
        for row in result.get("results", []):
            entry: dict[str, Any] = {}
            for field in row:
                entry[field["field"]] = field["value"]
            results.append(entry)

        return compact_cloudwatch_evidence(
            results,
            query=query,
            start_time=start_time,
            end_time=end_time,
            request_id=query_id,
            statistics={
                "records_matched": stats.get("recordsMatched", 0),
                "records_scanned": stats.get("recordsScanned", 0),
                "bytes_scanned": stats.get("bytesScanned", 0),
            },
            max_samples_per_group=max(1, min(samples_per_group, 5)),
        )
    except ClientError as exc:
        return {"error": f"CloudWatch API error: {exc.response['Error']['Message']}", "results": []}
    except BotoCoreError as exc:
        return {"error": f"AWS SDK error: {str(exc)}", "results": []}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_log_events(
    log_group_name: Annotated[str, "CloudWatch log group name."],
    log_stream_name: Annotated[str, "Specific CloudWatch log stream name."],
    start_time: Annotated[str | None, "Optional start time as ISO 8601 or relative string."] = None,
    end_time: Annotated[str | None, "Optional end time as ISO 8601 or relative string."] = None,
    limit: Annotated[int, "Maximum number of events to return."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return compact evidence for one exact stream; raw events are never returned."""
    group_error = _validate_log_groups([log_group_name])
    if group_error:
        return {"error": group_error, "events": []}
    effective_start = start_time or "-1h"
    effective_end = end_time or "now"
    time_range = _validate_time_range(effective_start, effective_end)
    if isinstance(time_range, str):
        return {"error": time_range}
    try:
        client = _get_logs_client(headers)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "events": []}
    kwargs: dict[str, Any] = {
        "logGroupName": log_group_name,
        "logStreamName": log_stream_name,
        "limit": max(1, min(limit, MAX_QUERY_RESULTS)),
        "startFromHead": True,
    }

    parsed_start, parsed_end = time_range
    kwargs["startTime"] = parsed_start
    kwargs["endTime"] = parsed_end

    try:
        response = client.get_log_events(**kwargs)
        events = [
            {
                "timestamp": event.get("timestamp"),
                "message": event.get("message", ""),
                "log_group": log_group_name,
                "log_stream": log_stream_name,
            }
            for event in response.get("events", [])
        ]
        return compact_cloudwatch_evidence(
            events,
            query="exact_log_stream",
            start_time=effective_start,
            end_time=effective_end,
        )
    except ClientError as exc:
        return {"error": f"CloudWatch API error: {exc.response['Error']['Message']}", "events": []}
    except BotoCoreError as exc:
        return {"error": f"AWS SDK error: {str(exc)}", "events": []}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def describe_log_groups(
    prefix: Annotated[str | None, "Optional log group name prefix filter."] = None,
    limit: Annotated[int, "Maximum number of log groups to return."] = 50,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this once to discover candidate log groups before running CloudWatch queries."""
    if not prefix or _validate_log_groups([prefix]):
        return {"error": "CloudWatch log group prefix must be explicitly allowed", "log_groups": []}
    try:
        client = _get_logs_client(headers)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "log_groups": []}
    kwargs: dict[str, Any] = {"limit": min(limit, 50)}
    if prefix:
        kwargs["logGroupNamePrefix"] = prefix

    try:
        response = client.describe_log_groups(**kwargs)
        groups = [
            {
                "name": group["logGroupName"],
                "stored_bytes": group.get("storedBytes", 0),
                "retention_days": group.get("retentionInDays"),
                "creation_time": group.get("creationTime"),
            }
            for group in response.get("logGroups", [])
        ]
        return {"log_groups": groups, "count": len(groups)}
    except ClientError as exc:
        return {"error": f"CloudWatch API error: {exc.response['Error']['Message']}", "log_groups": []}
    except BotoCoreError as exc:
        return {"error": f"AWS SDK error: {str(exc)}", "log_groups": []}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_alarm_log_groups(
    alarm_arn: Annotated[str, "Full validated CloudWatch alarm ARN from the alert."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Resolve one alarm to configured allowlisted log groups; call this instead of guessing a group name."""
    try:
        match = _validate_alarm_arn(alarm_arn, headers)
    except ValueError as exc:
        return {"error": str(exc), "log_groups": []}
    alarm_name = match.group("name")
    log_groups = list(ALARM_LOG_GROUP_MAPPINGS.get(alarm_name, ()))
    if not log_groups:
        log_groups, resolution_error = _resolve_dimension_log_groups(alarm_arn, headers)
        if resolution_error:
            return {"error": resolution_error, "log_groups": []}
        mapping_source = "alarm_dimensions"
    else:
        mapping_source = "platform_config"
    group_error = _validate_log_groups(log_groups)
    if group_error:
        return {"error": group_error, "log_groups": []}
    return {
        "provider": "cloudwatch",
        "alarm_arn": alarm_arn,
        "alarm_name": alarm_name,
        "log_groups": log_groups,
        "mapping_source": mapping_source,
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def describe_alarm(
    alarm_arn: Annotated[str, "Full CloudWatch alarm ARN."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return state, metric identity, period, and evaluation periods needed to scope the alert investigation."""
    try:
        client, alarm_name = _get_alarm_client(alarm_arn, headers)
        response = client.describe_alarms(AlarmNames=[alarm_name], MaxRecords=1)
        tags_response = client.list_tags_for_resource(ResourceARN=alarm_arn)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc)}
    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return {"error": "CloudWatch alarm was not found"}
    alarm = alarms[0]
    return {
        "provider": "cloudwatch",
        "alarm_arn": alarm_arn,
        "alarm_name": str(alarm.get("AlarmName") or alarm_name),
        "description": str(alarm.get("AlarmDescription") or ""),
        "state": str(alarm.get("StateValue") or "UNKNOWN"),
        "state_reason": _redact(alarm.get("StateReason")),
        "state_updated_timestamp": str(alarm.get("StateUpdatedTimestamp") or ""),
        "namespace": str(alarm.get("Namespace") or ""),
        "metric_name": str(alarm.get("MetricName") or ""),
        "dimensions": [
            {"name": str(item.get("Name") or ""), "value": _redact(item.get("Value"))}
            for item in alarm.get("Dimensions", [])
            if isinstance(item, dict)
        ],
        "tags": [
            {"key": str(item.get("Key") or ""), "value": _redact(item.get("Value"))}
            for item in tags_response.get("Tags", [])
            if isinstance(item, dict)
        ],
        "period": int(alarm.get("Period") or 0),
        "evaluation_periods": int(alarm.get("EvaluationPeriods") or 0),
        "threshold": alarm.get("Threshold"),
        "comparison_operator": str(alarm.get("ComparisonOperator") or ""),
        "treat_missing_data": str(alarm.get("TreatMissingData") or ""),
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_alarm_history(
    alarm_arn: Annotated[str, "Full CloudWatch alarm ARN."],
    start_time: Annotated[
        str,
        "Bounded history lookback used to identify the latest ALARM transition and recurrence; this is not automatically the log search window.",
    ] = "-24h",
    end_time: Annotated[str, "End time as ISO 8601 or now."] = "now",
    max_items: Annotated[int, "Maximum state updates to return."] = 50,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return state transitions used with alarm period/evaluation periods to derive a narrow log window."""
    time_range = _validate_time_range(start_time, end_time)
    if isinstance(time_range, str):
        return {"error": time_range, "history": []}
    try:
        client, alarm_name = _get_alarm_client(alarm_arn, headers)
        start_ms, end_ms = time_range
        response = client.describe_alarm_history(
            AlarmName=alarm_name,
            AlarmTypes=["MetricAlarm"],
            HistoryItemType="StateUpdate",
            StartDate=datetime.fromtimestamp(start_ms / 1000),
            EndDate=datetime.fromtimestamp(end_ms / 1000),
            MaxRecords=max(1, min(max_items, 100)),
            ScanBy="TimestampDescending",
        )
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "history": []}
    history = [
        {
            "timestamp": str(item.get("Timestamp") or ""),
            "summary": _redact(item.get("HistorySummary")),
            "type": str(item.get("HistoryItemType") or ""),
        }
        for item in response.get("AlarmHistoryItems", [])
        if isinstance(item, dict)
    ]
    return {
        "provider": "cloudwatch",
        "alarm_arn": alarm_arn,
        "start_time": start_time,
        "end_time": end_time,
        "history": history,
        "count": len(history),
        "truncated": bool(response.get("NextToken")),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-cloudwatch"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
