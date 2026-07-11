#!/usr/bin/env python3
"""Test plugin-local UserPromptSubmit hook.

Emits a deterministic marker so tests can assert the hook ran.
Exits 0 always.
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
                    "hook_name": "test_prompt_hook",
                    "hook_event": "UserPromptSubmit",
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

    result = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "PLATFORM_TEST_PLUGIN_PROMPT_HOOK_MARKER prompt=" + str(payload.get("prompt", ""))[:80]
            ),
        },
    }
    _emit_event("PLATFORM_TEST_PLUGIN_PROMPT_HOOK_MARKER")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
