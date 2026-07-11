"""Layer 0 — Message webhook helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from gateway.message import (  # noqa: E402
    _comment_response,
    _detect_gateway_shortcut,
    _render_help_text,
    _resolve_workflow,
    _strip_trigger_word,
    _verify_webhook_token,
)


def test_detect_gateway_shortcut_status() -> None:
    assert _detect_gateway_shortcut("status") == "status"
    assert _detect_gateway_shortcut("  STATUS  ") == "status"


def test_detect_gateway_shortcut_help() -> None:
    assert _detect_gateway_shortcut("help") == "help"
    assert _detect_gateway_shortcut("?") == "help"


def test_detect_gateway_shortcut_other_returns_none() -> None:
    assert _detect_gateway_shortcut("investigate something") is None
    assert _detect_gateway_shortcut("") is None


def test_strip_trigger_word_agent_prefix() -> None:
    assert _strip_trigger_word("@agent help", "@agent", {}) == "help"
    assert _strip_trigger_word("@Agent investigate", "@agent", {}) == "investigate"


def test_strip_trigger_word_no_prefix_preserves_text() -> None:
    assert _strip_trigger_word("raw message", "", {}) == "raw message"


def test_resolve_workflow_is_case_insensitive() -> None:
    routes = {"platform-test-channel": "platform-test"}
    assert _resolve_workflow("Platform-Test-Channel", routes) == "platform-test"
    assert _resolve_workflow("unknown", routes) is None


def test_render_help_text_includes_routes() -> None:
    out = _render_help_text({"alpha": "workflow-a", "beta": "workflow-b"})
    assert "#alpha" in out and "workflow-a" in out
    assert "#beta" in out and "workflow-b" in out


def test_render_help_text_empty() -> None:
    assert "No message workflow channels" in _render_help_text({})


def test_comment_response_shape() -> None:
    assert _comment_response("hi") == {"response_type": "comment", "text": "hi"}


def test_verify_webhook_token_skips_when_secret_unset() -> None:
    from shared.lib.config import settings

    with patch.object(settings, "message_outgoing_webhook_secret", ""):
        assert _verify_webhook_token("anything") is True


def test_verify_webhook_token_matches_secret() -> None:
    from shared.lib.config import settings

    with patch.object(settings, "message_outgoing_webhook_secret", "s3cr3t"):
        assert _verify_webhook_token("s3cr3t") is True
        assert _verify_webhook_token("wrong") is False
