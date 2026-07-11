"""Jira MCP Server — use for creating bug tickets from verified workflow findings."""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, extract_bearer_token, require_header, validate_base_url

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

bootstrap_platform_env()

BUG_ISSUE_TYPE = "Bug"
MAX_SUMMARY_LENGTH = 255

mcp = FastMCP("Jira MCP Server")


def _jira_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _validate_summary(value: str) -> str:
    summary = value.strip()
    if not summary:
        raise ValueError("Ticket title must not be empty")
    if len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError(f"Ticket title must be <= {MAX_SUMMARY_LENGTH} characters")
    return summary


def _validate_description(value: str) -> str:
    description = value.strip()
    if not description:
        raise ValueError("Ticket description must not be empty")
    return description


def _description_adf(text: str) -> dict[str, Any]:
    paragraphs = []
    for block in text.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        paragraph_text = "\n".join(lines)
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph_text}],
            }
        )
    if not paragraphs:
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        )
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs,
    }


def _project_key(value: str) -> str:
    project = value.strip()
    if not project:
        raise ValueError("Jira project key must not be empty")
    return project


def _raise_jira_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    detail = response.text.strip()
    if len(detail) > 800:
        detail = detail[:800]
    raise ValueError(f"Jira create issue failed with {response.status_code}: {detail}") from exc


def _create_issue_v3(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    project: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    response = client.post(
        f"{base_url}/rest/api/3/issue",
        headers=_jira_headers(token),
        json={
            "fields": {
                "project": {"key": project},
                "summary": title,
                "description": _description_adf(description),
                "issuetype": {"name": BUG_ISSUE_TYPE},
            }
        },
    )
    response.raise_for_status()
    return response.json()


def _create_issue_v2(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    project: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    response = client.post(
        f"{base_url}/rest/api/2/issue",
        headers=_jira_headers(token),
        json={
            "fields": {
                "project": {"key": project},
                "summary": title,
                "description": description,
                "issuetype": {"name": BUG_ISSUE_TYPE},
            }
        },
    )
    response.raise_for_status()
    return response.json()


@mcp.tool(annotations={"openWorldHint": True})
def create_bug_ticket(
    title: Annotated[str, "Short bug title for the Jira issue summary."],
    description: Annotated[str, "Detailed bug description with evidence, impact, and next action."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Create a Jira Bug ticket using request-scoped auth and routing headers."""
    token = extract_bearer_token(headers)
    if not token:
        raise ValueError("Jira token must be provided via the Authorization header as a bearer token")

    base_url = validate_base_url(
        require_header(headers, "x-jira-base-url", "Jira base URL"),
        header_name="x-jira-base-url",
    )
    project = _project_key(require_header(headers, "x-jira-project", "Jira project key"))
    summary = _validate_summary(title)
    body = _validate_description(description)

    with httpx.Client(timeout=30.0) as client:
        try:
            issue = _create_issue_v3(
                client,
                base_url=base_url,
                token=token,
                project=project,
                title=summary,
                description=body,
            )
            api_version = "3"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                _raise_jira_error(exc)
            try:
                issue = _create_issue_v2(
                    client,
                    base_url=base_url,
                    token=token,
                    project=project,
                    title=summary,
                    description=body,
                )
                api_version = "2"
            except httpx.HTTPStatusError as fallback_exc:
                _raise_jira_error(fallback_exc)

    issue_key = str(issue.get("key") or "")
    issue_id = str(issue.get("id") or "")
    browse_url = f"{base_url}/browse/{issue_key}" if issue_key else None
    return {
        "status": "created",
        "issue_id": issue_id,
        "issue_key": issue_key,
        "issue_url": browse_url,
        "api_version": api_version,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-jira"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
