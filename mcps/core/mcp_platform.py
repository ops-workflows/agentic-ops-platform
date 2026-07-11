"""Platform MCP Server — use for internal workflow handoffs and instruction-update PR proposals."""

from __future__ import annotations

import base64
import re
import time
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, get_env

bootstrap_platform_env()

GATEWAY_URL = get_env("GATEWAY_URL", "http://gateway:8080")
GITHUB_TOKEN = get_env("GITHUB_TOKEN", "")
GITHUB_REPO = get_env("GITHUB_REPO", "")
GITHUB_API = "https://api.github.com"
ALLOWED_PATH_PREFIXES = ("workflows/", "skills/")
ALLOWED_EXTENSIONS = (".md",)

mcp = FastMCP("Platform MCP Server")


def _github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _validate_skill_path(file_path: str) -> None:
    if ".." in file_path or file_path.startswith("/"):
        raise ValueError("Path must be relative and must not contain '..'")
    if not any(file_path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        raise ValueError(f"Updates are only allowed under: {', '.join(ALLOWED_PATH_PREFIXES)}")
    if not any(file_path.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise ValueError(f"Only these file types can be updated: {', '.join(ALLOWED_EXTENSIONS)}")


@mcp.tool(annotations={"openWorldHint": True})
def create_workflow_task(
    workflow: Annotated[str, "Target workflow name for the new task."],
    prompt: Annotated[str, "Prompt to enqueue for the target workflow."],
    message_channel: Annotated[str | None, "Optional message channel override for the follow-up task."] = None,
    message_thread: Annotated[str | None, "Optional message thread root override for the follow-up task."] = None,
    metadata: Annotated[dict[str, Any] | None, "Optional structured metadata for the created task."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this to enqueue a follow-up task in another Agentic Ops workflow when visible handoff text is not enough."""
    payload_metadata = dict(metadata or {})
    payload_metadata.setdefault("source", "workflow-handoff")
    if headers.get("x-task-id"):
        payload_metadata.setdefault("source_task_id", headers["x-task-id"])
    if headers.get("x-task-workflow"):
        payload_metadata.setdefault("source_workflow", headers["x-task-workflow"])

    default_channel = headers.get("x-message-channel") or None
    default_thread = headers.get("x-message-thread-id") or None
    if workflow == "documentation":
        default_channel = "documentation"

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{GATEWAY_URL}/tasks",
            json={
                "workflow": workflow,
                "prompt": prompt,
                "channel": "platform",
                "metadata": payload_metadata,
                "message_channel": message_channel or default_channel,
                "message_thread": message_thread or default_thread,
            },
        )
        response.raise_for_status()
        return response.json()


@mcp.tool(annotations={"openWorldHint": True})
def propose_skill_update(
    file_path: Annotated[str, "Repository-relative path to a skill or agent definition markdown file."],
    content: Annotated[str, "Complete updated file content."],
    title: Annotated[str, "Short PR title describing the change."],
    description: Annotated[str, "PR body describing the evidence and reasoning for the update."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this during reflection to propose a reusable skill or agent update as a GitHub PR."""
    _validate_skill_path(file_path)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"error": "GITHUB_TOKEN and GITHUB_REPO must be configured"}

    repo_api = f"{GITHUB_API}/repos/{GITHUB_REPO}"
    github_headers = _github_headers()

    with httpx.Client(timeout=30.0) as client:
        repo_response = client.get(repo_api, headers=github_headers)
        repo_response.raise_for_status()
        default_branch = repo_response.json()["default_branch"]

        ref_response = client.get(f"{repo_api}/git/ref/heads/{default_branch}", headers=github_headers)
        ref_response.raise_for_status()
        base_sha = ref_response.json()["object"]["sha"]

        slug = re.sub(r"[^a-zA-Z0-9-]", "-", file_path.split("/")[-1].replace(".md", ""))
        branch_name = f"reflect/{slug}-{int(time.time())}"
        client.post(
            f"{repo_api}/git/refs",
            headers=github_headers,
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        ).raise_for_status()

        existing = client.get(
            f"{repo_api}/contents/{file_path}",
            headers=github_headers,
            params={"ref": default_branch},
        )
        file_sha = existing.json().get("sha") if existing.status_code == 200 else None

        file_payload: dict[str, Any] = {
            "message": title,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch_name,
        }
        if file_sha:
            file_payload["sha"] = file_sha

        client.put(
            f"{repo_api}/contents/{file_path}",
            headers=github_headers,
            json=file_payload,
        ).raise_for_status()

        source_info = []
        if headers.get("x-task-id"):
            source_info.append(f"Task: `{headers['x-task-id']}`")
        if headers.get("x-task-workflow"):
            source_info.append(f"Workflow: `{headers['x-task-workflow']}`")
        source_section = "\n".join(source_info) if source_info else "Manual reflection"
        pr_body = (
            f"{description}\n\n---\n**Source**: {source_section}\n"
            "**Auto-generated** by agent reflection - requires human review before merge."
        )

        pr_response = client.post(
            f"{repo_api}/pulls",
            headers=github_headers,
            json={
                "title": f"[reflect] {title}",
                "head": branch_name,
                "base": default_branch,
                "body": pr_body,
            },
        )
        pr_response.raise_for_status()
        pr_data = pr_response.json()

    return {
        "status": "pr_created",
        "pr_number": pr_data["number"],
        "pr_url": pr_data["html_url"],
        "branch": branch_name,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-platform"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
