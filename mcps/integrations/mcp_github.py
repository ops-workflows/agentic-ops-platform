"""Read-only GitHub MCP for bounded code and existing-work investigation."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote, urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env
from shared.lib.github_app import GitHubAppError, github_app_connection, github_installation_token
from shared.lib.platform_secrets import load_mcp_server_config

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

PLATFORM_CONFIG_FILE = os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml")
AGE_IDENTITY = os.environ.get("AGE_IDENTITY", "")
_POLICY = load_mcp_server_config(PLATFORM_CONFIG_FILE, "github")
ALLOWED_HOSTS = frozenset(
    str(value).strip().lower() for value in _POLICY.get("allowed_hosts", []) if str(value).strip()
)
MAX_RESULTS = max(1, min(int(_POLICY.get("max_results") or 20), 50))
MAX_EXCERPT_LINES = max(1, min(int(_POLICY.get("max_excerpt_lines") or 200), 500))
MAX_FILE_BYTES = max(1_024, min(int(_POLICY.get("max_file_bytes") or 1_048_576), 5_242_880))
MAX_CONTEXT_CHARS = 16_384
MAX_QUERY_CHARS = 256
MAX_ISSUE_TITLE_CHARS = 256
MAX_ISSUE_BODY_CHARS = 65_536
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

mcp = FastMCP("GitHub MCP Server")


@dataclass(frozen=True)
class RepositoryContext:
    alias: str
    connection_name: str
    api_base_url: str
    owner: str
    repo: str
    allowed_paths: tuple[str, ...]
    issue_write: bool

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def _parse_repository_context(headers: dict[str, str], alias: str) -> RepositoryContext:
    if not _ALIAS_RE.fullmatch(alias):
        raise ValueError("GitHub repository alias has invalid syntax")
    raw = headers.get("x-github-repositories", "")
    if not raw or len(raw) > MAX_CONTEXT_CHARS:
        raise ValueError("GitHub repository context is missing or too large")
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub repository context must be valid JSON") from exc
    if not isinstance(configured, dict) or len(configured) > 16:
        raise ValueError("GitHub repository context must map at most 16 aliases")
    definition = configured.get(alias)
    if not isinstance(definition, dict):
        raise ValueError("GitHub repository alias is not configured for this workflow")

    connection_name = str(definition.get("connection") or "").strip()
    if not _ALIAS_RE.fullmatch(connection_name):
        raise ValueError("GitHub repository connection is not configured")
    try:
        connection = github_app_connection(
            connection_name,
            platform_config_file=PLATFORM_CONFIG_FILE,
            age_identity=AGE_IDENTITY or None,
        )
    except GitHubAppError as exc:
        raise ValueError(str(exc)) from exc
    parsed = urlparse(connection.api_base_url)
    if parsed.scheme != "https" or not ALLOWED_HOSTS or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("GitHub API host is not allowed by platform policy")
    owner = str(definition.get("owner") or "")
    repo = str(definition.get("repo") or "")
    if not _OWNER_REPO_RE.fullmatch(owner) or not _OWNER_REPO_RE.fullmatch(repo):
        raise ValueError("GitHub owner or repository has invalid syntax")
    raw_paths = definition.get("allowed_paths", [""])
    if not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > 32:
        raise ValueError("GitHub allowed_paths must be a non-empty list")
    allowed_paths = tuple(_normalize_path(str(path), allow_empty=True) for path in raw_paths)
    issue_write = definition.get("issue_write", False)
    if not isinstance(issue_write, bool):
        raise ValueError("GitHub issue_write must be true or false")
    return RepositoryContext(
        alias,
        connection_name,
        connection.api_base_url,
        owner,
        repo,
        allowed_paths,
        issue_write,
    )


def _normalize_path(path: str, *, allow_empty: bool = False) -> str:
    normalized = path.strip().strip("/")
    if not normalized and allow_empty:
        return ""
    if not normalized or ".." in normalized.split("/") or any(ord(char) < 32 for char in normalized):
        raise ValueError("GitHub path has invalid syntax")
    return normalized


def _validate_path(context: RepositoryContext, path: str) -> str:
    normalized = _normalize_path(path)
    if not any(
        not prefix or normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in context.allowed_paths
    ):
        raise ValueError("GitHub path is outside the workflow allowlist")
    return normalized


def _validate_ref(ref: str) -> str:
    value = ref.strip()
    if not _REF_RE.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("GitHub ref has invalid syntax")
    return value


def _validate_query(query: str) -> str:
    value = " ".join(query.split())
    if not value or len(value) > MAX_QUERY_CHARS or any(ord(char) < 32 for char in value):
        raise ValueError(f"GitHub query must contain 1-{MAX_QUERY_CHARS} safe characters")
    return value


def _validate_issue_text(value: str, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum or any(ord(char) < 9 for char in normalized):
        raise ValueError(f"GitHub issue {field} must contain at most {maximum} safe characters")
    return normalized


def _validate_labels(labels: list[str]) -> list[str]:
    if len(labels) > 10:
        raise ValueError("GitHub issue labels are limited to 10")
    return [_validate_issue_text(label, field="label", maximum=50) for label in labels]


def _request(
    context: RepositoryContext,
    path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        token = github_installation_token(
            context.connection_name,
            platform_config_file=PLATFORM_CONFIG_FILE,
            age_identity=AGE_IDENTITY or None,
        )
    except GitHubAppError as exc:
        raise ValueError(str(exc)) from exc
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method,
                f"{context.api_base_url}{path}",
                headers=request_headers,
                params=params,
                json=json_body,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"GitHub API request failed with status {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"GitHub API request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("GitHub API returned an unexpected response")
    return payload


def _repo_path(context: RepositoryContext, suffix: str = "") -> str:
    root = f"/repos/{quote(context.owner, safe='')}/{quote(context.repo, safe='')}"
    return f"{root}{suffix}"


def _search_query(context: RepositoryContext, query: str, kind: str, state: str, labels: list[str]) -> str:
    if state not in {"open", "closed", "all"}:
        raise ValueError("GitHub state must be open, closed, or all")
    terms = [f"repo:{context.full_name}", f"is:{kind}", _validate_query(query)]
    if state != "all":
        terms.append(f"state:{state}")
    for label in labels[:10]:
        safe_label = _validate_query(label)
        terms.append(f'label:"{safe_label}"')
    return " ".join(terms)


def _project_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": int(item.get("number") or 0),
        "title": str(item.get("title") or ""),
        "state": str(item.get("state") or ""),
        "url": str(item.get("html_url") or ""),
        "labels": [str(label.get("name") or "") for label in item.get("labels", []) if isinstance(label, dict)],
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "closed_at": str(item.get("closed_at") or ""),
        "body_excerpt": str(item.get("body") or "")[:1_000],
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_repository(
    repository: Annotated[str, "Workflow-configured repository alias."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return bounded metadata for one workflow-allowlisted repository."""
    try:
        context = _parse_repository_context(headers, repository)
        payload = _request(context, _repo_path(context))
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "repository": context.alias,
        "full_name": str(payload.get("full_name") or context.full_name),
        "default_branch": str(payload.get("default_branch") or ""),
        "visibility": str(payload.get("visibility") or ("private" if payload.get("private") else "public")),
        "archived": bool(payload.get("archived")),
        "updated_at": str(payload.get("updated_at") or ""),
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def resolve_commit(
    repository: Annotated[str, "Workflow-configured repository alias."],
    ref: Annotated[str, "Branch, tag, or commit ref to resolve."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Resolve a repository ref to an immutable commit SHA."""
    try:
        context = _parse_repository_context(headers, repository)
        safe_ref = _validate_ref(ref)
        payload = _request(context, _repo_path(context, f"/commits/{quote(safe_ref, safe='')}"))
    except ValueError as exc:
        return {"error": str(exc)}
    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    return {
        "repository": context.alias,
        "ref": safe_ref,
        "sha": str(payload.get("sha") or ""),
        "url": str(payload.get("html_url") or ""),
        "message": str(commit.get("message") or "")[:1_000],
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_file_excerpt(
    repository: Annotated[str, "Workflow-configured repository alias."],
    path: Annotated[str, "Repository-relative allowlisted file path."],
    commit_sha: Annotated[str, "Exact 40-character commit SHA."],
    start_line: Annotated[int, "First one-based line to return."],
    end_line: Annotated[int, "Last one-based line to return."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return a bounded text excerpt from one file at an immutable commit."""
    try:
        context = _parse_repository_context(headers, repository)
        safe_path = _validate_path(context, path)
        if not _COMMIT_RE.fullmatch(commit_sha):
            raise ValueError("GitHub file excerpts require an exact 40-character commit SHA")
        if start_line < 1 or end_line < start_line or end_line - start_line + 1 > MAX_EXCERPT_LINES:
            raise ValueError(f"GitHub excerpt must contain 1-{MAX_EXCERPT_LINES} lines")
        payload = _request(
            context,
            _repo_path(context, f"/contents/{quote(safe_path, safe='/')}"),
            params={"ref": commit_sha},
        )
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise ValueError("GitHub path is not a text file")
        content = base64.b64decode(payload["content"], validate=False)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"GitHub file exceeds the {MAX_FILE_BYTES}-byte limit")
        text = content.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        return {"error": str(exc)}
    lines = text.splitlines()
    selected = lines[start_line - 1 : end_line]
    return {
        "repository": context.alias,
        "commit_sha": commit_sha,
        "path": safe_path,
        "start_line": start_line,
        "end_line": start_line + len(selected) - 1,
        "excerpt": "\n".join(selected),
        "truncated": end_line < len(lines),
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def search_code(
    repository: Annotated[str, "Workflow-configured repository alias."],
    query: Annotated[str, "Bounded GitHub code-search terms."],
    max_results: Annotated[int, "Maximum candidates to return."] = 10,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Return bounded code candidates; use resolve_commit and get_file_excerpt to pin evidence."""
    try:
        context = _parse_repository_context(headers, repository)
        safe_query = _validate_query(query)
        limit = max(1, min(max_results, MAX_RESULTS))
        payload = _request(
            context,
            "/search/code",
            params={"q": f"repo:{context.full_name} {safe_query}", "per_page": limit},
        )
    except ValueError as exc:
        return {"error": str(exc), "matches": []}
    matches = []
    for item in payload.get("items", [])[:limit]:
        if not isinstance(item, dict):
            continue
        try:
            path = _validate_path(context, str(item.get("path") or ""))
        except ValueError:
            continue
        matches.append({"path": path, "url": str(item.get("html_url") or "")})
    return {
        "repository": context.alias,
        "query": safe_query,
        "matches": matches,
        "count": len(matches),
        "incomplete_results": bool(payload.get("incomplete_results")),
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def search_issues(
    repository: Annotated[str, "Workflow-configured repository alias."],
    query: Annotated[str, "Exact or normalized issue-search terms."],
    state: Annotated[str, "Issue state: open, closed, or all."] = "all",
    labels: Annotated[list[str] | None, "Optional labels, maximum 10."] = None,
    max_results: Annotated[int, "Maximum issue matches to return."] = 10,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Search issues only; a zero count means no match for this query, not no related work."""
    try:
        context = _parse_repository_context(headers, repository)
        search = _search_query(context, query, "issue", state, labels or [])
        limit = max(1, min(max_results, MAX_RESULTS))
        payload = _request(context, "/search/issues", params={"q": search, "per_page": limit})
    except ValueError as exc:
        return {"error": str(exc), "issues": []}
    issues = [_project_issue(item) for item in payload.get("items", [])[:limit] if isinstance(item, dict)]
    return {
        "repository": context.alias,
        "query": search,
        "issues": issues,
        "count": len(issues),
        "zero_result_semantics": "no_match_for_this_query" if not issues else "matches_found",
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def search_pull_requests(
    repository: Annotated[str, "Workflow-configured repository alias."],
    query: Annotated[str, "Exact or normalized pull-request search terms."],
    state: Annotated[str, "Pull-request state: open, closed, or all."] = "all",
    labels: Annotated[list[str] | None, "Optional labels, maximum 10."] = None,
    max_results: Annotated[int, "Maximum pull-request matches to return."] = 10,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Search pull requests separately and include authoritative merge status."""
    try:
        context = _parse_repository_context(headers, repository)
        search = _search_query(context, query, "pr", state, labels or [])
        limit = max(1, min(max_results, min(MAX_RESULTS, 10)))
        payload = _request(context, "/search/issues", params={"q": search, "per_page": limit})
        pull_requests = []
        for item in payload.get("items", [])[:limit]:
            if not isinstance(item, dict):
                continue
            projected = _project_issue(item)
            detail = _request(context, _repo_path(context, f"/pulls/{projected['number']}"))
            projected["merged"] = bool(detail.get("merged_at"))
            projected["merged_at"] = str(detail.get("merged_at") or "")
            projected["base_ref"] = str((detail.get("base") or {}).get("ref") or "")
            projected["head_sha"] = str((detail.get("head") or {}).get("sha") or "")
            pull_requests.append(projected)
    except ValueError as exc:
        return {"error": str(exc), "pull_requests": []}
    return {
        "repository": context.alias,
        "query": search,
        "pull_requests": pull_requests,
        "count": len(pull_requests),
        "zero_result_semantics": "no_match_for_this_query" if not pull_requests else "matches_found",
    }


@mcp.tool(annotations={"openWorldHint": True})
def create_issue(
    repository: Annotated[str, "Workflow-configured repository alias."],
    title: Annotated[str, "Issue title, maximum 256 characters."],
    body: Annotated[str, "Issue body, maximum 65536 characters."],
    labels: Annotated[list[str] | None, "Optional existing labels, maximum 10."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Create an issue in a repository whose workflow context explicitly enables issue writes."""
    try:
        context = _parse_repository_context(headers, repository)
        if not context.issue_write:
            raise ValueError("GitHub issue writes are disabled for this repository alias")
        payload = _request(
            context,
            _repo_path(context, "/issues"),
            method="POST",
            json_body={
                "title": _validate_issue_text(title, field="title", maximum=MAX_ISSUE_TITLE_CHARS),
                "body": _validate_issue_text(body, field="body", maximum=MAX_ISSUE_BODY_CHARS, allow_empty=True),
                "labels": _validate_labels(labels or []),
            },
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"repository": context.alias, "issue": _project_issue(payload)}


@mcp.tool(annotations={"openWorldHint": True})
def update_issue(
    repository: Annotated[str, "Workflow-configured repository alias."],
    issue_number: Annotated[int, "Positive issue number."],
    title: Annotated[str | None, "Optional replacement title."] = None,
    body: Annotated[str | None, "Optional replacement body."] = None,
    state: Annotated[str | None, "Optional state: open or closed."] = None,
    labels: Annotated[list[str] | None, "Optional replacement labels, maximum 10."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Update bounded fields on an issue when the repository alias enables issue writes."""
    try:
        context = _parse_repository_context(headers, repository)
        if not context.issue_write:
            raise ValueError("GitHub issue writes are disabled for this repository alias")
        if issue_number < 1:
            raise ValueError("GitHub issue number must be positive")
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = _validate_issue_text(title, field="title", maximum=MAX_ISSUE_TITLE_CHARS)
        if body is not None:
            changes["body"] = _validate_issue_text(body, field="body", maximum=MAX_ISSUE_BODY_CHARS, allow_empty=True)
        if state is not None:
            if state not in {"open", "closed"}:
                raise ValueError("GitHub issue state must be open or closed")
            changes["state"] = state
        if labels is not None:
            changes["labels"] = _validate_labels(labels)
        if not changes:
            raise ValueError("GitHub issue update requires at least one changed field")
        payload = _request(
            context,
            _repo_path(context, f"/issues/{issue_number}"),
            method="PATCH",
            json_body=changes,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"repository": context.alias, "issue": _project_issue(payload)}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-github"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
