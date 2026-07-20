"""Minimal background HTTP health endpoint for non-request-driven processes.

Connectors are long-running pollers/subscribers with no HTTP server of their
own. This starts a tiny stdlib-only `/health` listener on a daemon thread for
deployment health checks.
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def start_health_server(port: int | None = None) -> ThreadingHTTPServer:
    """Start a `/health` HTTP server on a daemon thread and return it."""
    resolved_port = port if port is not None else int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", resolved_port), _HealthHandler)  # noqa: S104
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on :%s/health", resolved_port)
    return server
