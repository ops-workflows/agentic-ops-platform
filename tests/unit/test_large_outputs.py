"""Unit tests for large-output / file-handling helpers.

Tests the runtime entrypoint's tool_result extraction + terminal payload
construction which truncate large outputs and preserve full content in
``large_parts`` for MinIO upload.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("TASK_ID", "test")
os.environ.setdefault("TASK_PROMPT", "test")

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

from runtime.session_entrypoint import (  # noqa: E402
    MAX_INLINE_SIZE,
    _build_terminal_event_payload,
    _extract_tool_result_message,
    _is_subagent_no_output_result,
)


def test_max_inline_size_threshold_is_10kb():
    assert MAX_INLINE_SIZE == 10 * 1024


class _FakeBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _FakeMessage:
    def __init__(self, blocks):
        self.content = blocks


def test_extract_tool_result_preview_truncates_to_2000():
    large = "x" * 50_000
    msg = _FakeMessage([_FakeBlock("tu_1", large, is_error=False)])
    result = _extract_tool_result_message(msg)
    assert result is not None
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_1"
    assert result["is_error"] is False
    # Preview is capped at 2000 chars to keep inline events small
    assert len(result["content_preview"]) == 2000


def test_extract_tool_result_error_flag():
    msg = _FakeMessage([_FakeBlock("tu_2", "boom", is_error=True)])
    result = _extract_tool_result_message(msg)
    assert result is not None
    assert result["is_error"] is True


def test_extract_tool_result_returns_none_for_message_without_blocks():
    assert _extract_tool_result_message(SimpleNamespace(content=[])) is None
    assert _extract_tool_result_message(SimpleNamespace(content=None)) is None


def test_extract_tool_result_none_when_no_tool_result_block():
    # content list without tool_use_id + content dual-attribute block
    bare = SimpleNamespace(some_other_field="x")
    msg = _FakeMessage([bare])
    assert _extract_tool_result_message(msg) is None


def test_build_terminal_event_payload_keeps_previews_and_large_parts():
    large_msg = "A" * 50_000
    large_result = "B" * 20_000
    payload = _build_terminal_event_payload(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "turns": 3,
            "total_messages": 4,
            "total_cost_usd": 0.002,
            "model_usage": {"test": {"input_tokens": 10}},
            "last_assistant_message": large_msg,
            "last_result_text": large_result,
        },
        error="timeout",
    )

    # Previews truncated to 2000
    assert len(payload["last_assistant_message_preview"]) == 2000
    assert len(payload["last_result_text_preview"]) == 2000
    # Full content retained separately for large-file upload
    assert payload["last_assistant_message"] == large_msg
    assert payload["last_result_text"] == large_result
    # Counters preserved
    assert payload["input_tokens"] == 10
    assert payload["turns"] == 3
    assert payload["error"] == "timeout"


def test_build_terminal_event_payload_omits_empty_large_parts():
    payload = _build_terminal_event_payload(
        {"input_tokens": 0, "output_tokens": 0, "turns": 0},
        error="cancelled",
    )
    assert "last_assistant_message" not in payload
    assert "last_result_text" not in payload
    assert payload["error"] == "cancelled"


def test_is_subagent_no_output_result_detects_marker():
    assert _is_subagent_no_output_result("Subagent completed but returned no output.") is True
    assert _is_subagent_no_output_result("something else") is False
