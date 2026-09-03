"""Unit tests for bounded Splunk evidence preprocessing."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from mcps.integrations.mcp_splunk import compact_splunk_evidence

pytestmark = pytest.mark.unit


def _reload_with_policy(monkeypatch, tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """mcps:
    config:
        splunk:
            max_results: 50
            max_window_hours: 24
            max_evidence_bytes: 8192
"""
    )
    monkeypatch.setenv("PLATFORM_CONFIG_FILE", str(config))
    import mcps.integrations.mcp_splunk as splunk

    return importlib.reload(splunk)


def test_compact_evidence_extracts_nested_exception_and_groups_equivalent_rows() -> None:
    def row(timestamp: str, request_id: str) -> dict[str, str]:
        return {
            "_raw": json.dumps(
                {
                    "timestamp": timestamp,
                    "source": "online-ui-api",
                    "severity": "ERROR",
                    "exception": json.dumps(
                        {
                            "exceptionClass": "GraphWriteException",
                            "detailMessage": f"write failed request_id={request_id}",
                        }
                    ),
                    "message": (
                        'updateNumber__checkpoint => HTTP POST "/v2/private/resources/12345?account=123456789012"\n'
                        "Traceback\nframe one\nframe two"
                    ),
                }
            )
        }

    payload = {
        "count": 2,
        "results": [
            row("2026-08-11T10:00:00Z", "a8098c1a-f86e-11da-bd1a-00112444be1e"),
            row("2026-08-11T10:01:00Z", "b8098c1a-f86e-11da-bd1a-00112444be1e"),
        ],
    }

    evidence = compact_splunk_evidence(
        payload,
        query="search index=online source=online-ui-api",
        earliest="-15m",
        latest="now",
    )

    assert evidence["total_results"] == 2
    assert len(evidence["groups"]) == 1
    group = evidence["groups"][0]
    assert group["count"] == 2
    assert group["first_seen"] == "2026-08-11T10:00:00Z"
    sample = group["samples"][0]
    assert sample["exception_class"] == "GraphWriteException"
    assert sample["detail"] == "write failed request_id=<id>"
    assert sample["operation"] == "updateNumber"
    assert sample["method"] == "POST"
    assert sample["endpoint"] == "/v2/private/resources/{id}"
    assert "123456789012" not in json.dumps(evidence)
    assert "_raw" not in json.dumps(evidence)


def test_compact_evidence_marks_invalid_nested_json_and_stack_truncation() -> None:
    evidence = compact_splunk_evidence(
        {
            "results": [
                {
                    "source": "online-ui-api",
                    "exception": "{invalid",
                    "message": "\n".join(f"frame {number}" for number in range(20)),
                }
            ]
        },
        query="search index=online",
        earliest="-1h",
        latest="now",
    )

    assert evidence["warnings"] == ["invalid_exception_json"]
    assert evidence["truncated"] is True
    assert evidence["groups"][0]["samples"][0]["stack_truncated"] is True


def test_search_logs_enforces_policy_and_compacts_transport_response(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    captured = {}

    def request(method, endpoint, **kwargs):
        captured.update({"method": method, "endpoint": endpoint, **kwargs})
        return {"results": [{"source": "online-ui-api", "message": "failure"}], "count": 1}

    monkeypatch.setattr(splunk, "_splunk_request", request)
    result = splunk.search_logs(
        "index=online source=online-ui-api",
        max_results=500,
        headers={"x-splunk-base-url": "https://splunk.example.com"},
    )

    assert result["provider"] == "splunk"
    assert result["total_results"] == 1
    assert "results" not in result
    assert captured["params"]["count"] == 50
    assert captured["params"]["earliest_time"] == "-15m"
    assert captured["params"]["latest_time"] == "now"


def test_compact_evidence_applies_shared_configured_redaction(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        splunk,
        "_REDACTION_PATTERNS",
        splunk.compile_redaction_patterns([{"pattern": r"customer-[A-Z0-9]+", "replacement": "<customer>"}]),
    )

    evidence = splunk.compact_splunk_evidence(
        {"results": [{"source": "online-ui-api", "message": "failed for customer-ABC123"}]},
        query="search index=online",
        earliest="-1h",
        latest="now",
    )

    assert evidence["groups"][0]["samples"][0]["stack_excerpt"] == "failed for <customer>"


def test_search_logs_accepts_text_and_spl(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    captured = []

    def request(_method, _endpoint, **kwargs):
        captured.append(kwargs)
        return {"results": [], "count": 0}

    monkeypatch.setattr(splunk, "_splunk_request", request)

    splunk.search_logs("checkout failed", headers={})
    splunk.search_logs("SEARCH index=online source=api", headers={})
    splunk.search_logs('| makeresults | eval message="ready"', headers={})

    assert [call["params"]["search"] for call in captured] == [
        "search checkout failed",
        "SEARCH index=online source=api",
        '| makeresults | eval message="ready"',
    ]
    assert "must not be empty" in splunk.search_logs("  ")["error"]
    assert "disallowed command" in splunk.search_logs("index=online | collect index=other")["error"]
    assert "earliest and latest parameters" in splunk.search_logs("index=online earliest=-7d")["error"]


def test_search_logs_rejects_invalid_or_oversized_time_window(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    assert "timezone-aware" in splunk.search_logs("index=online", earliest="yesterday")["error"]
    assert "24-hour limit" in splunk.search_logs("index=online", earliest="-7d")["error"]
    assert (
        "before latest"
        in splunk.search_logs(
            "index=online",
            earliest="2026-08-11T11:00:00Z",
            latest="2026-08-11T10:00:00Z",
        )["error"]
    )


def test_build_auth_headers_accepts_standard_bearer_header(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    assert splunk._build_auth_headers(
        object(),
        "https://splunk.example.com",
        {"authorization": "Bearer workflow-token"},
    ) == {"Authorization": "Bearer workflow-token"}


def test_parse_response_preserves_single_export_result(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    assert splunk._parse_response('{"result":{"_raw":"one event"}}') == {
        "results": [{"_raw": "one event"}],
        "count": 1,
    }


def test_build_auth_headers_uses_request_username_and_password(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sessionKey": "session-key"}

    class Client:
        def post(self, url, *, data):
            assert url == "https://splunk.example.com/services/auth/login"
            assert data["username"] == "service-user"
            assert data["password"] == "service-password"
            return Response()

    assert splunk._build_auth_headers(
        Client(),
        "https://splunk.example.com",
        {
            "x-splunk-username": "service-user",
            "x-splunk-password": "service-password",
        },
    ) == {"Authorization": "Splunk session-key"}

    monkeypatch.setattr(splunk, "SESSION_COOKIE_NAME", "proxy_session")
    assert splunk._build_auth_headers(
        Client(),
        "https://splunk.example.com",
        {
            "x-splunk-username": "service-user",
            "x-splunk-password": "service-password",
        },
    ) == {"Cookie": "proxy_session=session-key"}


def test_splunk_request_uses_workflow_base_url_without_host_policy(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    captured = {}

    class Response:
        content = b'{"results": []}'
        text = '{"results": []}'

        def raise_for_status(self):
            return None

    class Client:
        headers = {}

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, params):
            captured.update(url=url, params=params)
            return Response()

    monkeypatch.setattr(splunk.httpx, "Client", Client)
    monkeypatch.setattr(splunk, "_build_auth_headers", lambda *_args: {})

    result = splunk._splunk_request(
        "GET",
        "/services/search/jobs/export",
        headers={"x-splunk-base-url": "https://splunk-proxy.example.test/api"},
        params={"output_mode": "json"},
    )

    assert result == {"results": []}
    assert captured["url"] == "https://splunk-proxy.example.test/api/services/search/jobs/export"


def test_splunk_exposes_only_search_and_sid_results_tools(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    assert sorted(tool.name for tool in asyncio.run(splunk.mcp.list_tools())) == ["get_search_results", "search_logs"]
    assert callable(splunk.search_logs)
    assert callable(splunk.get_search_results)
    assert not hasattr(splunk, "parse_alert_url")
    assert not hasattr(splunk, "get_search_job")
    assert not hasattr(splunk, "run_saved_search")
    assert not hasattr(splunk, "get_alert_events")


def test_get_search_results_requires_final_job_and_compacts_rows(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    responses = [
        {
            "entry": [
                {
                    "content": {
                        "dispatchState": "DONE",
                        "isDone": True,
                        "eventCount": 4,
                        "resultCount": 2,
                        "earliestTime": "2026-08-11T10:00:00Z",
                        "latestTime": "2026-08-11T10:15:00Z",
                        "search": "search index=online source=online-ui-api",
                    }
                }
            ]
        },
        {"results": [{"source": "online-ui-api", "message": "failure"}], "count": 1},
    ]

    monkeypatch.setattr(splunk, "_splunk_request", lambda *_args, **_kwargs: responses.pop(0))
    evidence = splunk.get_search_results("scheduler__online_errors", headers={})

    assert evidence["finalized"] is True
    assert evidence["job"]["event_count"] == 4
    assert evidence["total_results"] == 1
    assert "results" not in evidence


def test_get_search_results_rejects_preview_job(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        splunk,
        "_splunk_request",
        lambda *_args, **_kwargs: {"entry": [{"content": {"dispatchState": "RUNNING", "isDone": False}}]},
    )

    result = splunk.get_search_results("scheduler__online_errors", headers={})

    assert "not successfully finalized" in result["error"]
    assert result["job"]["is_done"] is False


def test_compact_evidence_enforces_total_serialized_budget(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    payload = {
        "results": [
            {
                "_time": f"2026-08-11T10:{number:02d}:00Z",
                "source": "online-ui-api",
                "exception_class": f"Error{number}",
                "message": f"failure-{number} " + ("x" * 4_000),
            }
            for number in range(20)
        ]
    }

    evidence = splunk.compact_splunk_evidence(payload, query="search index=online", earliest="-1h", latest="now")

    assert len(json.dumps(evidence).encode()) <= 8_192
    assert evidence["truncated"] is True
    assert "serialized_budget_exceeded" in evidence["warnings"]
