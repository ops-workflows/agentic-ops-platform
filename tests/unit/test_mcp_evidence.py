"""Unit tests for shared compact evidence redaction."""

from __future__ import annotations

import pytest

from mcps.evidence import compile_redaction_patterns, redact_text

pytestmark = pytest.mark.unit


def test_shared_redaction_supports_defaults_and_configured_patterns() -> None:
    patterns = compile_redaction_patterns([{"pattern": r"customer-[A-Z0-9]+", "replacement": "<customer>"}])

    redacted = redact_text(
        "user@example.com 131052-308T 4111 1111 1111 1111 customer-ABC123",
        patterns,
    )

    assert redacted == "<email> <personal_id> <payment_card> <customer>"


def test_shared_redaction_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        compile_redaction_patterns("not-a-list")
    with pytest.raises(ValueError, match="pattern is invalid"):
        compile_redaction_patterns([{"pattern": "["}])
