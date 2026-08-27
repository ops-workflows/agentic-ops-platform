from __future__ import annotations

import pytest

from mcps.integrations import mcp_jira

pytestmark = pytest.mark.unit


def test_create_bug_ticket_uses_only_jira_api_v3(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class Client:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 30.0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def create_issue_v3(_client, **kwargs):
        captured.update(kwargs)
        return {"id": "10001", "key": "OPS-42"}

    monkeypatch.setattr(mcp_jira.httpx, "Client", Client)
    monkeypatch.setattr(mcp_jira, "_create_issue_v3", create_issue_v3)

    result = mcp_jira.create_bug_ticket(
        "Production API error",
        "Requests fail after deployment and require investigation.",
        headers={
            "authorization": "Bearer jira-token",
            "x-jira-base-url": "https://jira.example.com",
            "x-jira-project": "OPS",
        },
    )

    assert captured == {
        "base_url": "https://jira.example.com",
        "token": "jira-token",
        "project": "OPS",
        "title": "Production API error",
        "description": "Requests fail after deployment and require investigation.",
    }
    assert result == {
        "status": "created",
        "issue_id": "10001",
        "issue_key": "OPS-42",
        "issue_url": "https://jira.example.com/browse/OPS-42",
    }
