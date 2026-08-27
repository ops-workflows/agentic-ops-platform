"""Deterministic alert envelope and coalescing identity tests."""

from __future__ import annotations

from shared.lib.alert_envelope import parse_alert_envelope


def test_aws_state_transitions_share_coalesce_key() -> None:
    firing = parse_alert_envelope(
        """Status: firing
Alertname: production-api-gatewayAlarm
Labels: alertarn=arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-api-gatewayAlarm, environment=production
"""
    )
    recovery = parse_alert_envelope(
        """Status: resolved
Alertname: production-api-gatewayAlarm
Labels: alertarn=arn:aws:cloudwatch:eu-west-1:123456789012:alarm:production-api-gatewayAlarm, environment=production
"""
    )

    assert firing is not None
    assert recovery is not None
    assert firing.state == "firing"
    assert recovery.state == "recovery"
    assert firing.account == "123456789012"
    assert firing.region == "eu-west-1"
    assert firing.environment == "production"
    assert firing.coalesce_key("online-alerts-investigator") == recovery.coalesce_key("online-alerts-investigator")


def test_splunk_dispatch_ids_do_not_change_coalesce_key() -> None:
    first = parse_alert_envelope(
        "Splunk alert: Online-UI-API errors (https://splunk.example/app/search/@go?sid=scheduler__one)"
    )
    second = parse_alert_envelope(
        "Splunk alert: Online-UI-API errors (https://splunk.example/app/search/@go?sid=scheduler__two)"
    )

    assert first is not None
    assert second is not None
    assert first.identity == "Online-UI-API errors"
    assert first.coalesce_key("online-alerts-investigator") == second.coalesce_key("online-alerts-investigator")


def test_unsupported_message_has_no_alert_identity() -> None:
    assert parse_alert_envelope("@agent investigate this manually") is None
