#!/usr/bin/env python3
"""Test plugin-local SubagentStop hook.

Writes a marker line on stderr so the runtime hook event pipeline picks it up.
Exits 0.
"""

from __future__ import annotations

import contextlib
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
                    "hook_name": "test_subagent_stop_hook",
                    "hook_event": "SubagentStop",
                    "status": "success",
                    "detail": detail,
                },
            },
            timeout=2.5,
        )


def main() -> int:
    with contextlib.suppress(Exception):
        sys.stdin.read()
    detail = "PLATFORM_TEST_PLUGIN_SUBAGENT_STOP_HOOK_MARKER"
    sys.stderr.write(detail + "\n")
    _emit_event(detail)
    print('{"status": "ok"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
