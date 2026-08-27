"""Unit tests for bounded Splunk evidence preprocessing."""

from __future__ import annotations

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
            allowed_hosts: [splunk.example.com]
            allowed_indexes: [online, online-audit]
            max_results: 50
            max_window_hours: 24
            max_evidence_bytes: 8192
            saved_searches:
                online-api-errors: nobody/search/Online-UI-API-errors
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
    assert captured["params"]["earliest_time"] == "-1h"
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


def test_search_logs_rejects_missing_disallowed_or_unsafe_index(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    assert "explicit index" in splunk.search_logs("source=online-ui-api")["error"]
    assert "not allowed" in splunk.search_logs("index=secret source=logs")["error"]
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
    ) == {"Cookie": "splunkd_8000=session-key"}


def test_parse_alert_url_accepts_only_configured_host_and_safe_sid(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)

    parsed = splunk.parse_alert_url("https://splunk.example.com/en-GB/app/search/@go?sid=scheduler__online_errors")

    assert parsed["app"] == "search"
    assert parsed["sid"] == "scheduler__online_errors"
    assert (
        "not allowed"
        in splunk.parse_alert_url("https://other.example.com/en-GB/app/search/@go?sid=scheduler__online_errors")[
            "error"
        ]
    )
    assert (
        "valid sid"
        in splunk.parse_alert_url("https://splunk.example.com/en-GB/app/search/@go?sid=../../unsafe")["error"]
    )


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


def test_run_saved_search_uses_configured_alias_and_returns_compact_statistics(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    responses = [
        {
            "entry": [
                {
                    "content": {
                        "description": "Online API errors",
                        "search": "index=online source=online-ui-api severity=ERROR",
                        "disabled": False,
                        "cron_schedule": "*/5 * * * *",
                        "dispatch.earliest_time": "-10m",
                        "dispatch.latest_time": "now",
                        "alert_type": "number of events",
                        "alert_threshold": "0",
                        "actions": "webhook",
                    }
                }
            ]
        },
        {
            "results": [
                {"_time": "2026-08-11T10:00:00Z", "source": "online-ui-api", "message": "failure"},
                {"_time": "2026-08-11T10:01:00Z", "source": "online-ui-api", "message": "failure"},
            ],
            "count": 2,
        },
    ]
    captured = []

    def request(method, endpoint, **kwargs):
        captured.append((method, endpoint, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(splunk, "_splunk_request", request)
    evidence = splunk.run_saved_search(
        "online-api-errors",
        "-15m",
        "now",
        headers={},
    )

    assert evidence["total_results"] == 2
    assert evidence["groups"][0]["count"] == 2
    assert evidence["groups"][0]["first_seen"] == "2026-08-11T10:00:00Z"
    assert evidence["groups"][0]["last_seen"] == "2026-08-11T10:01:00Z"
    assert "fingerprint" not in evidence["groups"][0]
    assert evidence["saved_search"]["alias"] == "online-api-errors"
    assert evidence["saved_search"]["name"] == "Online-UI-API-errors"
    assert captured[0][1].endswith("/nobody/search/saved/searches/Online-UI-API-errors")
    assert captured[-1][1] == "/services/search/jobs/export"
    assert captured[-1][2]["params"]["earliest_time"] == "-15m"


def test_run_saved_search_rejects_unconfigured_name(monkeypatch, tmp_path) -> None:
    splunk = _reload_with_policy(monkeypatch, tmp_path)
    assert "not configured" in splunk.run_saved_search("unknown", headers={})["error"]


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
