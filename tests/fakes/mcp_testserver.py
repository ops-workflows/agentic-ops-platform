"""FastMCP test server.

Provides four synthetic MCP tools used across platform scenario tests:

- `echo_headers`        — returns the request headers received
- `return_large_result` — returns a large payload to exercise large-output handling
- `store_marker`        — records a marker string; tests can read it back
- `fail_with_error`     — always raises to exercise error paths

Served over streamable HTTP so the runtime's .mcp.json HTTP clients work
unchanged.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx
from fastapi import FastAPI

try:
    from fastmcp import FastMCP
except Exception:  # pragma: no cover - fastmcp optional for unit tests
    FastMCP = None  # type: ignore[assignment]


class TestMCPServer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._markers: list[str] = []
        self._recent_headers: dict[str, str] = {}
        self._built = self._build()
        self.app: FastAPI = self._built["app"]

    # ── Test helpers ──────────────────────────────────────────

    def markers(self) -> list[str]:
        with self._lock:
            return list(self._markers)

    def last_headers(self) -> dict[str, str]:
        with self._lock:
            return dict(self._recent_headers)

    def reset(self) -> None:
        with self._lock:
            self._markers.clear()
            self._recent_headers.clear()

    # ── Build ─────────────────────────────────────────────────

    def _build(self) -> dict[str, Any]:
        if FastMCP is None:
            # Fallback: expose a minimal FastAPI stub so service tests can still
            # import the module. Tool calls will 404.
            app = FastAPI(title="Test MCP (stub)")
            return {"app": app, "mcp": None}

        mcp = FastMCP("platform-test-mcp")

        @mcp.tool()
        def echo_headers() -> dict[str, str]:
            """Return the most recently observed request headers."""
            with self._lock:
                headers = dict(self._recent_headers)
            return {
                key: ("<redacted>" if any(token in key.lower() for token in ("secret", "token", "auth")) else value)
                for key, value in headers.items()
            }

        @mcp.tool()
        def return_large_result(size_bytes: int = 64 * 1024) -> str:
            """Return a large payload to exercise large-output handling."""
            return "A" * max(0, int(size_bytes))

        @mcp.tool()
        def store_marker(marker: str) -> dict[str, Any]:
            """Store a marker string tests can read back."""
            with self._lock:
                self._markers.append(str(marker))
            return {"stored": marker, "count": len(self._markers)}

        @mcp.tool()
        def fail_with_error(reason: str = "scripted") -> str:
            raise RuntimeError(f"scripted failure: {reason}")

        @mcp.tool()
        async def post_message(
            channel_id: str = "",
            message: str = "",
            text: str = "",
            thread_id: str = "",
        ) -> dict[str, Any]:
            api_url = self.last_headers().get("x-message-api-url", "")
            if not api_url:
                raise RuntimeError("x-message-api-url header missing")

            body: dict[str, Any] = {
                "channel_id": channel_id or self.last_headers().get("x-message-channel-id", "") or "",
                "message": message or text,
            }
            if thread_id:
                body["root_id"] = thread_id

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{api_url.rstrip('/')}/api/v4/posts", json=body)
                response.raise_for_status()
                return response.json()

        @mcp.tool()
        async def create_task(
            workflow: str,
            prompt: str,
            message_channel: str = "",
            message_thread: str = "",
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            from shared.lib.db import async_session_factory
            from shared.lib.task_queue import create_task as queue_create_task

            async with async_session_factory() as session:
                task = await queue_create_task(
                    session,
                    workflow=workflow,
                    prompt=prompt,
                    message_channel=message_channel or self.last_headers().get("x-message-channel", "") or None,
                    message_thread=message_thread or self.last_headers().get("x-message-thread-id", "") or None,
                    metadata=metadata
                    or {"channel_id": self.last_headers().get("x-message-channel-id", "test-channel-id")},
                )
                return {"id": str(task.id), "workflow": task.workflow, "status": task.status}

        @mcp.tool()
        async def retain(
            bank: str = "platform-test-bank",
            text: str = "",
            content: str = "",
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            hindsight_url = self.last_headers().get("x-hindsight-url", "")
            if not hindsight_url:
                raise RuntimeError("x-hindsight-url header missing")

            payload = {
                "text": text or content,
                "content": content or text,
                "metadata": metadata or {},
                "document_id": f"retain-{uuid.uuid4()}",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{hindsight_url.rstrip('/')}/v1/banks/{bank}/retain", json=payload)
                response.raise_for_status()
                return response.json()

        app = mcp.http_app()

        # FastMCP's http_app returns a Starlette app that does not accept
        # the decorator-style @app.middleware(). Use BaseHTTPMiddleware +
        # add_middleware instead to capture inbound headers for assertions.
        from starlette.middleware.base import BaseHTTPMiddleware

        outer = self

        class _CaptureHeaders(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                with outer._lock:
                    outer._recent_headers = dict(request.headers)
                return await call_next(request)

        app.add_middleware(_CaptureHeaders)

        return {"app": app, "mcp": mcp}


def build_test_mcp_server() -> TestMCPServer:
    return TestMCPServer()
