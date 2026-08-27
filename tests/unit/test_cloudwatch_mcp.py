"""Unit tests for CloudWatch MCP platform policy."""

from __future__ import annotations

import importlib
import json

import pytest
import yaml

pytestmark = pytest.mark.unit


def _reload_with_policy(monkeypatch, tmp_path):
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "mcps": {
                    "config": {
                        "cloudwatch": {
                            "allowed_regions": ["eu-west-1"],
                            "allowed_account_ids": ["123456789012"],
                            "allowed_log_group_prefixes": [
                                "/aws/lambda/production-",
                                "/aws/apigateway/production-",
                            ],
                            "alarm_dimension_log_group_templates": {
                                "FunctionName": ["/aws/lambda/{value}"],
                                "ApiName": ["/aws/apigateway/{value}"],
                            },
                            "max_results": 50,
                            "max_window_hours": 24,
                            "max_scan_bytes": 1024,
                            "max_evidence_bytes": 8192,
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setenv("PLATFORM_CONFIG_FILE", str(config))
    import mcps.integrations.mcp_cloudwatch as cloudwatch

    return importlib.reload(cloudwatch)


def test_get_logs_client_requires_allowed_region_and_account(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    credentials = {
        "x-aws-access-key-id": "access-key",
        "x-aws-secret-access-key": "secret-key",
    }

    with pytest.raises(ValueError, match="12-digit"):
        cloudwatch._get_logs_client({"x-aws-region": "eu-west-1", **credentials})
    with pytest.raises(ValueError, match="region is not allowed"):
        cloudwatch._get_logs_client({"x-aws-region": "us-east-1", "x-aws-account-id": "123456789012", **credentials})
    with pytest.raises(ValueError, match="credentials must be provided"):
        cloudwatch._get_logs_client({"x-aws-region": "eu-west-1", "x-aws-account-id": "123456789012"})
    with pytest.raises(ValueError, match="account is not allowed"):
        cloudwatch._get_logs_client({"x-aws-region": "eu-west-1", "x-aws-account-id": "184285746512", **credentials})


def test_get_logs_client_verifies_caller_identity(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    logs_client = object()

    class Session:
        def __init__(self, **kwargs):
            assert kwargs == {
                "aws_access_key_id": "access-key",
                "aws_secret_access_key": "secret-key",
                "aws_session_token": "session-token",
                "region_name": "eu-west-1",
            }

        def client(self, service):
            if service == "sts":
                return type("Sts", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})()
            assert service == "logs"
            return logs_client

    monkeypatch.setattr(cloudwatch.boto3.session, "Session", Session)

    assert (
        cloudwatch._get_logs_client(
            {
                "x-aws-region": "eu-west-1",
                "x-aws-account-id": "123456789012",
                "x-aws-access-key-id": "access-key",
                "x-aws-secret-access-key": "secret-key",
                "x-aws-session-token": "session-token",
            }
        )
        is logs_client
    )


def test_log_tools_reject_unapproved_groups_before_aws_call(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cloudwatch,
        "_get_logs_client",
        lambda _headers: pytest.fail("AWS client should not be created"),
    )

    assert "not allowed" in cloudwatch.search_logs("fields @message", ["/aws/lambda/uat-api"])["error"]
    assert "not allowed" in cloudwatch.get_log_events("/aws/lambda/uat-api", "stream")["error"]
    assert "explicitly allowed" in cloudwatch.describe_log_groups(prefix=None)["error"]


def test_alarm_log_groups_come_only_from_configured_exact_mapping(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(cloudwatch, "ALLOWED_LOG_GROUPS", frozenset({"/custom/production-api"}))
    monkeypatch.setattr(
        cloudwatch,
        "ALARM_LOG_GROUP_MAPPINGS",
        {"production-api-gatewayAlarm": ("/custom/production-api",)},
    )
    headers = {"x-aws-region": "eu-west-1", "x-aws-account-id": "123456789012"}

    mapped = cloudwatch.get_alarm_log_groups(
        "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-api-gatewayAlarm",
        headers=headers,
    )
    missing = cloudwatch.get_alarm_log_groups(
        "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-otherAlarm",
        headers=headers,
    )

    assert mapped["log_groups"] == ["/custom/production-api"]
    assert mapped["mapping_source"] == "platform_config"
    assert "credentials must be provided" in missing["error"]


def test_alarm_log_groups_resolve_from_configured_dimensions(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)

    class AlarmClient:
        def describe_alarms(self, **kwargs):
            assert kwargs == {"AlarmNames": ["production-api-errors"], "MaxRecords": 1}
            return {
                "MetricAlarms": [
                    {
                        "Dimensions": [
                            {"Name": "FunctionName", "Value": "production-api-lambda"},
                            {"Name": "ApiName", "Value": "production-api"},
                        ]
                    }
                ]
            }

    monkeypatch.setattr(
        cloudwatch,
        "_get_alarm_client",
        lambda _arn, _headers: (AlarmClient(), "production-api-errors"),
    )
    resolved = cloudwatch.get_alarm_log_groups(
        "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-api-errors",
        headers={"x-aws-region": "eu-west-1", "x-aws-account-id": "123456789012"},
    )

    assert resolved["log_groups"] == [
        "/aws/lambda/production-api-lambda",
        "/aws/apigateway/production-api",
    ]
    assert resolved["mapping_source"] == "alarm_dimensions"


def test_compact_evidence_groups_equivalent_redacted_events(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    rows = [
        {
            "@timestamp": "2026-08-11T10:00:00Z",
            "@log": "/aws/lambda/production-api",
            "@message": (
                "ValidationException request_id=a8098c1a-f86e-11da-bd1a-00112444be1e "
                "POST /v2/resources/12345 account 123456789012"
            ),
        },
        {
            "@timestamp": "2026-08-11T10:01:00Z",
            "@log": "/aws/lambda/production-api",
            "@message": (
                "ValidationException request_id=b8098c1a-f86e-11da-bd1a-00112444be1e "
                "POST /v2/resources/67890 account 123456789012"
            ),
        },
    ]

    evidence = cloudwatch.compact_cloudwatch_evidence(
        rows,
        query="fields @timestamp, @message",
        start_time="-15m",
        end_time="now",
        request_id="query-1",
    )

    assert evidence["provider"] == "cloudwatch"
    assert len(evidence["groups"]) == 1
    assert evidence["groups"][0]["count"] == 2
    assert "fingerprint" not in evidence["groups"][0]
    sample = evidence["groups"][0]["samples"][0]
    assert sample["exception_class"] == "ValidationException"
    assert sample["endpoint"] == "/v2/resources/{id}"
    assert "12345" not in sample["message_excerpt"]
    assert "123456789012" not in str(evidence)
    assert "@message" not in sample


def test_compact_evidence_applies_shared_configured_redaction(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cloudwatch,
        "_REDACTION_PATTERNS",
        cloudwatch.compile_redaction_patterns([{"pattern": r"customer-[A-Z0-9]+", "replacement": "<customer>"}]),
    )

    evidence = cloudwatch.compact_cloudwatch_evidence(
        [{"@message": "failed for customer-ABC123"}],
        query="fields @message",
        start_time="-1h",
        end_time="now",
    )

    assert evidence["groups"][0]["samples"][0]["message_excerpt"] == "failed for <customer>"


def test_search_logs_rejects_invalid_window_and_unmask(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)

    assert (
        "relative"
        in cloudwatch.search_logs("fields @message", ["/aws/lambda/production-api"], start_time="yesterday")["error"]
    )
    assert (
        "24-hour limit"
        in cloudwatch.search_logs("fields @message", ["/aws/lambda/production-api"], start_time="-7d")["error"]
    )
    assert (
        "unmask" in cloudwatch.search_logs("fields @message | unmask @message", ["/aws/lambda/production-api"])["error"]
    )


def test_search_logs_returns_compact_evidence(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)

    class Logs:
        def start_query(self, **kwargs):
            assert kwargs["limit"] == 50
            return {"queryId": "query-1"}

        def get_query_results(self, **_kwargs):
            return {
                "status": "Complete",
                "results": [
                    [
                        {"field": "@timestamp", "value": "2026-08-11T10:00:00Z"},
                        {"field": "@message", "value": "RuntimeError POST /v2/resources/123"},
                    ]
                ],
                "statistics": {"recordsMatched": 1, "recordsScanned": 2, "bytesScanned": 100},
            }

    monkeypatch.setattr(cloudwatch, "_get_logs_client", lambda _headers: Logs())
    evidence = cloudwatch.search_logs(
        "fields @timestamp, @message",
        ["/aws/lambda/production-api"],
        limit=500,
        headers={},
    )

    assert evidence["request"]["query_id"] == "query-1"
    assert evidence["total_results"] == 1
    assert "results" not in evidence


def test_search_logs_rejects_missing_or_excessive_scan_statistics(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)

    class Logs:
        def __init__(self, statistics):
            self.statistics = statistics

        def start_query(self, **_kwargs):
            return {"queryId": "query-1"}

        def get_query_results(self, **_kwargs):
            return {"status": "Complete", "results": [], "statistics": self.statistics}

    monkeypatch.setattr(cloudwatch, "_get_logs_client", lambda _headers: Logs({}))
    result = cloudwatch.search_logs("fields @message", ["/aws/lambda/production-api"], headers={})
    assert "required scan statistics" in result["error"]

    monkeypatch.setattr(
        cloudwatch,
        "_get_logs_client",
        lambda _headers: Logs({"recordsMatched": 1, "recordsScanned": 10, "bytesScanned": 1025}),
    )
    result = cloudwatch.search_logs("fields @message", ["/aws/lambda/production-api"], headers={})
    assert "1024-byte limit" in result["error"]


def test_alarm_tools_validate_arn_and_project_state_history(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    alarm_arn = "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-apiAlarm"

    class AlarmClient:
        def describe_alarms(self, **kwargs):
            assert kwargs["AlarmNames"] == ["production-apiAlarm"]
            return {
                "MetricAlarms": [
                    {
                        "AlarmName": "production-apiAlarm",
                        "StateValue": "ALARM",
                        "StateReason": "threshold breached for account 123456789012",
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Errors",
                        "Dimensions": [{"Name": "FunctionName", "Value": "production-api"}],
                        "Period": 60,
                        "EvaluationPeriods": 1,
                        "Threshold": 1.0,
                        "ComparisonOperator": "GreaterThanThreshold",
                    }
                ]
            }

        def describe_alarm_history(self, **kwargs):
            assert kwargs["HistoryItemType"] == "StateUpdate"
            return {
                "AlarmHistoryItems": [
                    {
                        "Timestamp": "2026-08-11T10:00:00Z",
                        "HistorySummary": "Alarm updated from OK to ALARM",
                        "HistoryItemType": "StateUpdate",
                    }
                ]
            }

        def list_tags_for_resource(self, **kwargs):
            assert kwargs["ResourceARN"] == alarm_arn
            return {"Tags": [{"Key": "Environment", "Value": "production"}]}

    monkeypatch.setattr(
        cloudwatch,
        "_get_alarm_client",
        lambda _arn, _headers: (AlarmClient(), "production-apiAlarm"),
    )
    alarm = cloudwatch.describe_alarm(alarm_arn, headers={})
    history = cloudwatch.get_alarm_history(alarm_arn, headers={})

    assert alarm["state"] == "ALARM"
    assert alarm["namespace"] == "AWS/Lambda"
    assert alarm["tags"] == [{"key": "Environment", "value": "production"}]
    assert "123456789012" not in alarm["state_reason"]
    assert history["count"] == 1
    assert history["history"][0]["type"] == "StateUpdate"


def test_alarm_arn_must_match_header_region_and_account(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    headers = {"x-aws-region": "eu-west-1", "x-aws-account-id": "123456789012"}

    with pytest.raises(ValueError, match="region does not match"):
        cloudwatch._get_alarm_client("arn:aws:cloudwatch:us-east-1:123456789012:alarm:production-apiAlarm", headers)
    with pytest.raises(ValueError, match="account does not match"):
        cloudwatch._get_alarm_client("arn:aws:cloudwatch:eu-west-1:184285746512:alarm:production-apiAlarm", headers)


def test_compact_evidence_enforces_total_serialized_budget(monkeypatch, tmp_path) -> None:
    cloudwatch = _reload_with_policy(monkeypatch, tmp_path)
    rows = [
        {
            "@timestamp": f"2026-08-11T10:{number:02d}:00Z",
            "@log": "/aws/lambda/production-api",
            "@message": f"Error{number} " + ("x" * 4_000),
        }
        for number in range(20)
    ]

    evidence = cloudwatch.compact_cloudwatch_evidence(rows, query="fields @message", start_time="-1h", end_time="now")

    assert len(json.dumps(evidence).encode()) <= 8_192
    assert evidence["truncated"] is True
    assert "serialized_budget_exceeded" in evidence["warnings"]
