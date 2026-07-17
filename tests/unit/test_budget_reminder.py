"""Unit tests for turn/time budget reminder logic.

Tests ``_should_send_ask_user_question_reminder`` and related reminder
helpers from the runtime entrypoint.
"""

from __future__ import annotations

import importlib
import os
import time

# The entrypoint reads MAX_TURNS / RUNTIME_TIMEOUT_SEC / reminder flags at
# import time. Set them before importing.
os.environ.setdefault("TASK_ID", "test")
os.environ.setdefault("TASK_PROMPT", "test")
os.environ["MAX_TURNS"] = "20"
os.environ["RUNTIME_TIMEOUT_SEC"] = "600"
os.environ["ASK_USER_QUESTION_REMINDER_ENABLED"] = "true"
os.environ["ASK_USER_QUESTION_REMINDER_MIN_TURNS"] = "5"
os.environ["ASK_USER_QUESTION_REMINDER_TURN_RATIO"] = "0.7"
os.environ["ASK_USER_QUESTION_REMINDER_TIME_RATIO"] = "0.75"
os.environ["ASK_USER_QUESTION_REMINDER_RECENT_QUESTION_TURN_WINDOW"] = "3"

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_entrypoint():
    """Re-import the entrypoint so module-level env vars are picked up."""
    import runtime.session_entrypoint as sep

    importlib.reload(sep)
    yield


def _get_sep():
    import runtime.session_entrypoint as sep

    return sep


# ── Reminder gating ──────────────────────────────────────────


def test_reminder_suppressed_when_disabled(monkeypatch):
    sep = _get_sep()
    monkeypatch.setattr(sep, "ASK_USER_QUESTION_REMINDER_ENABLED", False)
    should, _ = sep._should_send_ask_user_question_reminder({"turns": 50}, query_started_at=time.monotonic() - 1000)
    assert should is False


def test_reminder_suppressed_when_already_sent():
    sep = _get_sep()
    should, _ = sep._should_send_ask_user_question_reminder(
        {"turns": 50, "ask_user_question_reminder_sent": True},
        query_started_at=time.monotonic() - 1000,
    )
    assert should is False


def test_reminder_suppressed_when_result_text_present():
    sep = _get_sep()
    # Claude has already produced a final-result block; no need to remind.
    should, _ = sep._should_send_ask_user_question_reminder(
        {"turns": 50, "last_result_text": "final answer"},
        query_started_at=time.monotonic() - 1000,
    )
    assert should is False


def test_terminal_error_prefers_captured_model_response():
    sep = _get_sep()
    assert (
        sep._terminal_error_text(
            "Claude Code returned an error result: success",
            {"last_result_text": "API Error: model context exceeded"},
        )
        == "API Error: model context exceeded"
    )


def test_reminder_suppressed_below_min_turns():
    sep = _get_sep()
    # MIN_TURNS env was set to 5 above.
    should, _ = sep._should_send_ask_user_question_reminder(
        {"turns": 3},
        query_started_at=time.monotonic() - 1000,
    )
    assert should is False


def test_reminder_suppressed_if_recent_question():
    sep = _get_sep()
    # Recent question within window (3 turns).
    should, _ = sep._should_send_ask_user_question_reminder(
        {"turns": 15, "last_ask_user_question_turn": 13},
        query_started_at=time.monotonic() - 1000,
    )
    assert should is False


def test_reminder_fires_on_turn_budget():
    sep = _get_sep()
    # MAX_TURNS=20 * 0.7 = 14 → at turn 15 we're over the turn threshold
    should, ctx = sep._should_send_ask_user_question_reminder(
        {"turns": 15},
        query_started_at=time.monotonic() - 1,
    )
    assert should is True
    assert ctx["trigger"] == "turn_budget"
    assert ctx["turns"] == 15


def test_reminder_fires_on_time_budget():
    sep = _get_sep()
    # RUNTIME_TIMEOUT_SEC=600 * 0.75 = 450 seconds threshold
    # Use turns < turn threshold (14) but elapsed >= 450
    should, ctx = sep._should_send_ask_user_question_reminder(
        {"turns": 10},
        query_started_at=time.monotonic() - 500,
    )
    assert should is True
    assert ctx["trigger"] == "time_budget"


def test_reminder_text_is_nonempty():
    sep = _get_sep()
    text = sep._ask_user_question_reminder_text()
    assert "AskUserQuestion" in text
    assert len(text) > 50


def test_subagent_no_output_retry_text():
    sep = _get_sep()
    text = sep._subagent_no_output_retry_text()
    assert "retry" in text.lower()
    assert "narrower" in text.lower()


# ── AskUserQuestion response parsing ─────────────────────────────────────


def test_parse_question_response_single_choice_by_number():
    sep = _get_sep()
    question = {
        "question": "Which?",
        "options": [{"label": "Apple"}, {"label": "Banana"}, {"label": "Cherry"}],
    }
    assert sep._parse_question_response("2", question) == "Banana"


def test_parse_question_response_free_text_when_no_number():
    sep = _get_sep()
    question = {"question": "Why?", "options": [{"label": "A"}, {"label": "B"}]}
    assert sep._parse_question_response("it's broken", question) == "it's broken"


def test_parse_question_response_multi_select():
    sep = _get_sep()
    question = {
        "question": "Which?",
        "multiSelect": True,
        "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
    }
    assert sep._parse_question_response("1, 3", question) == "A, C"


def test_parse_question_response_out_of_range_falls_through():
    sep = _get_sep()
    question = {"question": "Which?", "options": [{"label": "A"}]}
    # "5" is out of range, so it falls through to free-text
    assert sep._parse_question_response("5", question) == "5"


@pytest.mark.asyncio
async def test_resume_admission_accepts_direct_running_transition(monkeypatch):
    """The scheduler may consume resume_pending before the runtime polls it."""

    sep = _get_sep()

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"status": "running"}

    class FakeClient:
        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(sep, "_get_client", fake_get_client)

    await sep._wait_for_resume_admission(reason="user_input", timeout_sec=1)
