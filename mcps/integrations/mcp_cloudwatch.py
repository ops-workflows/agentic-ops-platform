"""CloudWatch Logs MCP Server — use for AWS infrastructure logs and known CloudWatch log groups when Splunk is not the right source."""

from __future__ import annotations

import logging
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

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

REGION_PATTERN = re.compile(r"^[a-z0-9-]+$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")

mcp = FastMCP("CloudWatch Logs MCP Server")


def _get_logs_client(headers: dict[str, str]):
    region = headers.get("x-aws-region", "").strip()
    if not region:
        raise ValueError("AWS region must be provided via the x-aws-region header")
    if not REGION_PATTERN.match(region):
        raise ValueError("x-aws-region must contain only lowercase letters, digits, and hyphens")

    session = boto3.session.Session(region_name=region)
    expected_account = headers.get("x-aws-account-id", "").strip()
    if expected_account:
        if not ACCOUNT_PATTERN.match(expected_account):
            raise ValueError("x-aws-account-id must be a 12-digit AWS account ID")
        actual_account = session.client("sts").get_caller_identity()["Account"]
        if actual_account != expected_account:
            raise ValueError(
                f"AWS caller identity account {actual_account} does not match x-aws-account-id {expected_account}"
            )

    return session.client("logs")


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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def search_logs(
    query: Annotated[str, "CloudWatch Logs Insights query string."],
    log_group_names: Annotated[list[str], "One or more CloudWatch log groups to query."],
    start_time: Annotated[str, "Start time as ISO 8601 or relative like -1h."] = "-1h",
    end_time: Annotated[str, "End time as ISO 8601 or 'now'."] = "now",
    limit: Annotated[int, "Maximum number of results to return."] = 100,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this for bounded CloudWatch Logs Insights queries when you already know the relevant log groups."""
    try:
        client = _get_logs_client(headers)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "results": []}
    safe_limit = min(limit, 10000)
    start_ms = _parse_relative_time(start_time) or int((time.time() - 3600) * 1000)
    end_ms = _parse_relative_time(end_time) or int(time.time() * 1000)

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

        results: list[dict[str, Any]] = []
        for row in result.get("results", []):
            entry: dict[str, Any] = {}
            for field in row:
                entry[field["field"]] = field["value"]
            results.append(entry)

        stats = result.get("statistics", {})
        return {
            "results": results,
            "count": len(results),
            "statistics": {
                "records_matched": stats.get("recordsMatched", 0),
                "records_scanned": stats.get("recordsScanned", 0),
                "bytes_scanned": stats.get("bytesScanned", 0),
            },
        }
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
    """Use this only after you know the exact log stream and need raw events for targeted inspection."""
    try:
        client = _get_logs_client(headers)
    except (ValueError, BotoCoreError, ClientError) as exc:
        return {"error": str(exc), "events": []}
    kwargs: dict[str, Any] = {
        "logGroupName": log_group_name,
        "logStreamName": log_stream_name,
        "limit": min(limit, 10000),
        "startFromHead": True,
    }

    parsed_start = _parse_relative_time(start_time)
    parsed_end = _parse_relative_time(end_time)
    if parsed_start:
        kwargs["startTime"] = parsed_start
    if parsed_end:
        kwargs["endTime"] = parsed_end

    try:
        response = client.get_log_events(**kwargs)
        events = [
            {
                "timestamp": event.get("timestamp"),
                "message": event.get("message", ""),
                "ingestionTime": event.get("ingestionTime"),
            }
            for event in response.get("events", [])
        ]
        return {"events": events, "count": len(events)}
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-cloudwatch"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
