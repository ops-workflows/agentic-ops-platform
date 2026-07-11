#!/usr/bin/env python3
"""Test plugin-local PreToolUse hook.

Emits a marker line on stderr so the runtime hook event pipeline can
record it. Always allows the tool to proceed (exit 0, empty body).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

import httpx


def _emit_event(detail: str) -> None:
    gateway_event_url = os.environ.get("GATEWAY_EVENT_URL", "")
    task_id = os.environ.get("TASK_ID", "")
    if not gateway_event_url or not task_id:
        return
    with contextlib.suppress(Exception):
        httpx.post(
            gateway_event_url,
            json={
                "task_id": task_id,
                "event_type": "hook_event",
                "data": {
                    "hook_name": "test_pretool_hook",
                    "hook_event": "PreToolUse",
                    "status": "success",
                    "detail": detail,
                },
            },
            timeout=2.5,
        )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    tool_name = str(payload.get("tool_name") or payload.get("name") or "?")
    detail = f"PLATFORM_TEST_PLUGIN_PRETOOL_HOOK_MARKER tool={tool_name}"
    sys.stderr.write(detail + "\n")
    _emit_event(detail)
    print(json.dumps({"status": "ok"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
