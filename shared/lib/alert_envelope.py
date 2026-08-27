"""Deterministic alert identity parsing for message ingress."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

ALERT_ENVELOPE_VERSION = 1

_ALERT_NAME_PATTERN = re.compile(r"(?im)^Alertname:\s*([^\r\n]+)")
_AWS_ALARM_ARN_PATTERN = re.compile(
    r"arn:aws:cloudwatch:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):alarm:(?P<name>[^,\s]+)",
    re.IGNORECASE,
)
_ENVIRONMENT_PATTERN = re.compile(r"(?im)^Labels:.*?\benvironment=([^,\s]+)")
_STATUS_PATTERN = re.compile(r"(?im)^Status:\s*([^\r\n]+)")
_SPLUNK_ALERT_PATTERN = re.compile(r"(?im)^Splunk alert:\s*([^\r\n(]+)")


@dataclass(frozen=True)
class AlertEnvelope:
    version: int
    source: str
    identity: str
    state: str
    account: str = ""
    region: str = ""
    environment: str = ""

    def as_metadata(self) -> dict[str, object]:
        return asdict(self)

    def coalesce_key(self, workflow: str) -> str:
        canonical = "\x1f".join(
            (
                workflow.strip().lower(),
                self.source,
                self.account,
                self.region,
                self.environment,
                self.identity.lower(),
                str(self.version),
            )
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"alert:v{self.version}:{digest}"


def _canonical_state(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"alarm", "alert", "firing", "triggered"}:
        return "firing"
    if normalized in {"ok", "resolved", "recovered", "recovery"}:
        return "recovery"
    return "unknown"


def parse_alert_envelope(text: str) -> AlertEnvelope | None:
    """Parse a supported alert body without model interpretation."""
    body = text.strip()
    if not body:
        return None

    splunk_match = _SPLUNK_ALERT_PATTERN.search(body)
    if splunk_match:
        identity = splunk_match.group(1).strip()
        if not identity:
            return None
        return AlertEnvelope(
            version=ALERT_ENVELOPE_VERSION,
            source="splunk",
            identity=identity,
            state="firing",
        )

    arn_match = _AWS_ALARM_ARN_PATTERN.search(body)
    name_match = _ALERT_NAME_PATTERN.search(body)
    if not arn_match and not name_match:
        return None
    identity = (name_match.group(1) if name_match else arn_match.group("name")).strip()
    if not identity:
        return None
    environment_match = _ENVIRONMENT_PATTERN.search(body)
    state_match = _STATUS_PATTERN.search(body)
    return AlertEnvelope(
        version=ALERT_ENVELOPE_VERSION,
        source="aws",
        identity=identity,
        state=_canonical_state(state_match.group(1) if state_match else ""),
        account=arn_match.group("account") if arn_match else "",
        region=arn_match.group("region").lower() if arn_match else "",
        environment=environment_match.group(1).strip().lower() if environment_match else "",
    )
