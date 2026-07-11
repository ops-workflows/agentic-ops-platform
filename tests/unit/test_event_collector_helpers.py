"""Layer 0 — event collector token coercion."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from gateway.event_collector import (  # noqa: E402
    _coerce_total_tokens,
    _extract_incremental_tokens,
)


def test_coerce_total_tokens_from_dict() -> None:
    assert _coerce_total_tokens({"total_tokens": 42}) == 42
    assert _coerce_total_tokens({"input_tokens": 10, "output_tokens": 5}) == 15


def test_coerce_total_tokens_from_json_string() -> None:
    assert _coerce_total_tokens('{"total_tokens": 99}') == 99


def test_coerce_total_tokens_from_regex_fallback() -> None:
    # Non-JSON, non-literal string with recognizable total_tokens field
    assert _coerce_total_tokens("something total_tokens: 7 something") == 7


def test_coerce_total_tokens_none_on_empty() -> None:
    assert _coerce_total_tokens(None) is None
    assert _coerce_total_tokens({}) is None
    assert _coerce_total_tokens("") is None


def test_extract_incremental_tokens_from_task_progress() -> None:
    event_data = {
        "messages": [
            {
                "type": "system",
                "subtype": "task_progress",
                "data": {"usage": {"total_tokens": 123}},
            }
        ]
    }
    assert _extract_incremental_tokens(event_data) == 123


def test_extract_incremental_tokens_prefers_later_result() -> None:
    event_data = {
        "messages": [
            {"type": "system", "subtype": "task_progress", "data": {"usage": {"total_tokens": 10}}},
            {"type": "result", "usage": {"total_tokens": 25}},
        ]
    }
    assert _extract_incremental_tokens(event_data) == 25


def test_extract_incremental_tokens_none_when_missing() -> None:
    assert _extract_incremental_tokens({}) is None
    assert _extract_incremental_tokens({"messages": "not-a-list"}) is None
