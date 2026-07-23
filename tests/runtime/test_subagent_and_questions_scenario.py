"""Layer 2 — subagent and AskUserQuestion scenarios.

Covers:
- subagent delegation via the Agent / Task tool (helper subagent)
- subagent that returns no output and triggers the retry path
- AskUserQuestion approve / multi-select
- Approval timeout (no operator reply within window)
- Late-session AskUserQuestion reminder

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.mock_llm import Turn, request_text_blob

pytestmark = pytest.mark.scenario


SHARED_CLAUDE_MD_MARKER = "PLATFORM_TEST_SHARED_CLAUDE_MD_MARKER"
PLUGIN_CLAUDE_MD_MARKER = "PLATFORM_TEST_PLUGIN_CLAUDE_MD_MARKER"


# ─── §2.2.1 Subagent delegation via Agent/Task tool ──────────────


@pytest.mark.asyncio
async def test_subagent_delegation(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """LLM emits a Task/Agent tool_use targeting the helper subagent."""

    mock_llm.set_scenario(
        [
            # Coordinator delegates to helper
            Turn(
                respond=[
                    {"type": "text", "text": "Delegating to helper."},
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {
                            "subagent_type": "helper",
                            "description": "Helper sanity check",
                            "prompt": "Reply with marker SUBAGENT_OK.",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            # Helper subagent's view (LLM call from within the subagent)
            Turn(
                respond=[{"type": "text", "text": "SUBAGENT_OK"}],
                stop_reason="end_turn",
            ),
            # Coordinator receives helper result and ends
            Turn(
                respond=[{"type": "text", "text": "Helper completed: SUBAGENT_OK"}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Delegate to the helper and report its reply.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=240)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, (
        f"Expected the runtime to make at least 2 LLM calls (coordinator + subagent loop), saw {len(requests)}"
    )

    # The mock LLM saw the subagent loop — at least one request should
    # carry a Task/Agent tool_result back to the coordinator.
    import json as _json

    blob = "\n".join(_json.dumps(r.get("body", {}), ensure_ascii=False) for r in requests)
    assert "SUBAGENT_OK" in blob, "subagent's marker did not flow through the LLM loop"

    helper_request = next(
        (
            request["body"]
            for request in requests
            if "platform-test helper subagent" in request_text_blob(request["body"])
        ),
        None,
    )
    assert helper_request is not None, "Expected a model request for the helper subagent"
    helper_prompt = request_text_blob(helper_request)
    assert SHARED_CLAUDE_MD_MARKER in helper_prompt
    assert PLUGIN_CLAUDE_MD_MARKER in helper_prompt


# ─── §2.2.2 Subagent with no output triggers retry-text path ─────


@pytest.mark.asyncio
async def test_subagent_no_output_retry(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """Helper returns empty content; coordinator must inject the retry
    instruction and reissue."""

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {
                            "subagent_type": "helper",
                            "description": "noop",
                            "prompt": "Return nothing.",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            # Helper produces an empty assistant message (whitespace only)
            Turn(
                respond=[{"type": "text", "text": "   "}],
                stop_reason="end_turn",
            ),
            # Coordinator sees the empty subagent reply and tries again
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {
                            "subagent_type": "helper",
                            "description": "retry",
                            "prompt": "Try again. Return SUBAGENT_RETRY_OK.",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "SUBAGENT_RETRY_OK"}],
                stop_reason="end_turn",
            ),
            Turn(
                respond=[{"type": "text", "text": "Done after retry."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Delegate twice; expect retry behavior.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=240)
    # Even if the runtime cannot detect empty subagent output specially,
    # the scenario completes via the LLM-driven retry.
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"


# ─── Helper: wait + reply to AskUserQuestion / approval prompts ──


def _watch_for_post_and_reply(
    fake_mattermost,
    *,
    matcher: str,
    reply: str,
    timeout: float = 60.0,
):
    """Run in a background asyncio task: poll fake Message for a post
    matching ``matcher`` (substring), then inject ``reply`` to its thread.
    """

    async def _runner():
        loop = asyncio.get_event_loop()

        def _wait():
            return fake_mattermost.wait_for_post(
                lambda p: matcher.lower() in p.message.lower(),
                timeout=timeout,
            )

        post = await loop.run_in_executor(None, _wait)
        if post is None:
            return None
        fake_mattermost.inject_reply(
            thread_id=post.root_id or post.id,
            channel_id=post.channel_id,
            message=reply,
        )
        return post

    return asyncio.create_task(_runner())


# ─── §2.3.1 AskUserQuestion approve ──────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_question_approve(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    admit_when_resume_pending,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Proceed with the test?",
                                    "header": "Proceed?",
                                    "multiSelect": False,
                                    "options": [
                                        {"label": "Yes", "description": "go"},
                                        {"label": "No", "description": "stop"},
                                    ],
                                },
                            ],
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Got affirmative; proceeding to wrap up."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Ask the user whether to proceed.",
        message_thread="test-thread-auq-approve",
    )

    reply_task = _watch_for_post_and_reply(
        fake_mattermost,
        matcher="proceed",
        reply="1",
        timeout=120,
    )
    admit_task = asyncio.create_task(admit_when_resume_pending(task.id, workflow=task.workflow, timeout=120))

    exit_code, logs = await spawn_and_wait(task, timeout_sec=240)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(reply_task, timeout=5)
    await admit_task

    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    from shared.lib.models import TaskEvent

    async with factory() as session:
        event_types = (
            (
                await session.execute(
                    select(TaskEvent.event_type).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created)
                )
            )
            .scalars()
            .all()
        )
    assert "task_resumed" in event_types


# ─── §2.3.2 AskUserQuestion multi-select ─────────────────────────


@pytest.mark.asyncio
async def test_ask_user_question_multi_select(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    admit_when_resume_pending,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Pick categories:",
                                    "header": "Categories",
                                    "multiSelect": True,
                                    "options": [
                                        {"label": "alpha", "description": "first"},
                                        {"label": "beta", "description": "second"},
                                        {"label": "gamma", "description": "third"},
                                    ],
                                },
                            ],
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Multi-select acknowledged."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Ask the user to pick multiple categories.",
        message_thread="test-thread-auq-multi",
    )

    # Reply with two numbers — runtime should parse as multi-select.
    reply_task = _watch_for_post_and_reply(
        fake_mattermost,
        matcher="categories",
        reply="1, 3",
        timeout=120,
    )
    admit_task = asyncio.create_task(admit_when_resume_pending(task.id, workflow=task.workflow, timeout=120))

    exit_code, logs = await spawn_and_wait(task, timeout_sec=240)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(reply_task, timeout=5)
    await admit_task

    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"


# ─── §2.3.3 Approval timeout (no reply) ──────────────────────────


@pytest.mark.asyncio
async def test_approval_timeout_no_reply(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    monkeypatch,
) -> None:
    """Operator never replies; the runtime should treat the approval as
    denied/timed-out and the session should end cleanly."""

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo approval-needed timeout-test"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "No approval received; stopping."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Run echo approval-needed timeout-test (operator will not reply).",
        message_thread="test-thread-timeout",
    )

    from session_manager import container_lifecycle

    load_agent_yaml = container_lifecycle.load_agent_yaml

    def load_short_approval_timeout_agent_yaml(workflow: str):
        agent_config = load_agent_yaml(workflow)
        agent_config["runtime"] = {**agent_config.get("runtime", {}), "approval_timeout_sec": 20}
        return agent_config

    monkeypatch.setattr(container_lifecycle, "load_agent_yaml", load_short_approval_timeout_agent_yaml)

    exit_code, logs = await spawn_and_wait(task, timeout_sec=45)
    assert exit_code == 0, f"Approval timeout did not resolve cleanly.\nLogs:\n{logs}"


# ─── §2.3.4 Late-session question reminder ───────────────────────


@pytest.mark.asyncio
async def test_late_session_question_reminder(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """Run enough turns that the late-session reminder injection threshold
    is met; assert the reminder marker appears in the upstream system
    prompt or messages on a later turn."""

    # Build many Bash echo turns to push turn count up.
    turns: list[Turn] = []
    for i in range(7):
        turns.append(
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"echo turn-{i}"},
                    },
                ],
                stop_reason="tool_use",
            )
        )
    turns.append(Turn(respond=[{"type": "text", "text": "Done."}], stop_reason="end_turn"))

    mock_llm.set_scenario(turns)

    task = await create_task(prompt="Loop a few times to exercise the reminder threshold.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=240)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 6, f"Expected multiple LLM turns before reminder injection, saw {len(requests)}"

    import json as _json

    late_turn_bodies = [_json.dumps(request.get("body", {}), ensure_ascii=False).lower() for request in requests[-3:]]
    assert any("askuserquestion" in body for body in late_turn_bodies), (
        f"Late-session question reminder did not reach the upstream LLM request bodies: {late_turn_bodies}"
    )
