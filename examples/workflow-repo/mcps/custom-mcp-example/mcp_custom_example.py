"""Example custom MCP server — copy this file to start a new one.

Rename the module (e.g. mcp_billing.py), replace the example tool with your
own, and read any instance policy from `mcps.config.<server_id>` in
platform-config.yaml via `load_mcp_server_config`. See mcps/README.md in this
directory for the remaining wiring steps (deployment override, .mcp.json,
mcps.enabled).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, extract_bearer_token, require_header, validate_base_url
from shared.lib.platform_secrets import load_mcp_server_config

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

bootstrap_platform_env()

# Instance-specific policy from platform-config.yaml's mcps.config.custom-example.
# Replace "custom-example" with your server's id once you rename this file.
POLICY = load_mcp_server_config(os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml"), "custom-example")
DEFAULT_LIMIT = int(POLICY.get("default_limit") or 10)

mcp = FastMCP("Custom Example MCP Server")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def example_lookup(
    query: Annotated[str, "What to look up in the external system."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Look up something in an external system using per-request auth headers.

    Auth and instance context (base URL, token) come from the workflow's
    .mcp.json headers, expanded from ${VAR} at runtime — never from
    mcps.config, and never from the model's tool-call arguments.
    """
    token = extract_bearer_token(headers)
    if not token:
        raise ValueError("A bearer token must be provided via the Authorization header")

    base_url = validate_base_url(
        require_header(headers, "x-custom-example-base-url", "Custom example base URL"),
        header_name="x-custom-example-base-url",
    )

    # Replace with a real call, e.g. an httpx.Client request to base_url.
    return {"base_url": base_url, "query": query, "limit": DEFAULT_LIMIT, "results": []}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-custom-example"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
