from __future__ import annotations

import pytest

from mcps.core import mcp_message

pytestmark = pytest.mark.unit


def test_handoff_rejects_invalid_gateway_before_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = False

    def fail_if_posted(**_kwargs):
        nonlocal posted
        posted = True
        return {"success": True}

    monkeypatch.setattr(mcp_message, "GATEWAY_URL", "gateway:8080")
    monkeypatch.setattr(mcp_message, "_post_message", fail_if_posted)

    result = mcp_message.handoff_task(
        workflow="another-workflow",
        prompt="Investigate this follow-up",
        text="Assigning follow-up",
        headers={},
    )

    assert result == {"error": "GATEWAY_URL must be an absolute http:// or https:// URL"}
    assert posted is False


def test_gateway_tasks_url_accepts_absolute_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_message, "GATEWAY_URL", "http://gateway:8080/")

    assert mcp_message._gateway_tasks_url() == "http://gateway:8080/tasks"
