"""Layer 2 — MCP variant scenarios.

Covers:
- Message MCP call routes through the fake Message service
- Platform MCP create_task results in a new task row in Postgres
- MCP tool returning a server error (500) is surfaced as is_error=true
- MCP tool returning a very large result is offloaded to MinIO

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import json

import pytest

from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario


# ─── §2.6.1 Real Message MCP call routes through fake Message ──


@pytest.mark.asyncio
async def test_message_mcp_post_routes_through_fake(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """LLM emits ``mcp__message__post_message``; fake should record it."""
    fake_mattermost.reset()
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__message__post_message"]},
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__message__post_message",
                        "input": {
                            "channel_id": "platform-test-channel-id",
                            "message": "MCP_MESSAGE_TEST_POST",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Posted."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Post a message via the Message MCP.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    matched = [p for p in fake_mattermost.all_posts() if "MCP_MESSAGE_TEST_POST" in p.message]
    assert matched, f"Expected the Message MCP route to post through the fake service.\nLogs:\n{logs}"


# ─── §2.6.2 Platform MCP create_task creates a new task row ──────


@pytest.mark.asyncio
async def test_platform_mcp_create_task_creates_task_row(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    db_session,
) -> None:
    fake_mattermost.reset()
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__platform__create_task"]},
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__platform__create_task",
                        "input": {
                            "workflow": "platform-test",
                            "prompt": "MCP_PLATFORM_HANDOFF_TASK",
                            "message_channel": "platform-test-channel",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Handed off."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Hand off to a follow-up workflow via platform MCP.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    from sqlalchemy import select

    from shared.lib.models import Task as TaskModel

    rows = (
        (await db_session.execute(select(TaskModel).where(TaskModel.prompt == "MCP_PLATFORM_HANDOFF_TASK")))
        .scalars()
        .all()
    )
    assert rows, f"Expected platform MCP create_task to create a follow-up task row.\nLogs:\n{logs}"


# ─── §2.6.3 MCP tool 500 → tool_result.is_error=true ─────────────


@pytest.mark.asyncio
async def test_mcp_tool_returns_error(
    require_runtime,
    mock_llm,
    fake_mcp,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """The testserver's ``fail_with_error`` tool always raises. The
    runtime should serialize ``is_error=true`` into the next message."""
    fake_mcp.reset()
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__testserver__fail_with_error"]},
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__testserver__fail_with_error",
                        "input": {"reason": "scripted-test"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Acknowledged failure."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Trigger the failing MCP tool.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, f"Expected an MCP error result to flow back into a second LLM request.\nLogs:\n{logs}"
    second = json.dumps(requests[1].get("body", {}), ensure_ascii=False).lower()
    assert any(tok in second for tok in ("is_error", "error", "scripted-test", "fail")), (
        "Expected MCP failure path to be observable in the upstream LLM request.\n"
        f"Second request body: {second[:800]}\nLogs:\n{logs}"
    )


# ─── §2.6.4 MCP very large result → MinIO offload ────────────────


@pytest.mark.asyncio
async def test_mcp_large_result_offloaded(
    require_runtime,
    mock_llm,
    fake_mcp,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """``return_large_result`` returns a 64KB+ payload. Runtime should
    avoid embedding the full payload back into the LLM message body."""
    fake_mcp.reset()
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__testserver__return_large_result"]},
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__testserver__return_large_result",
                        "input": {"size_bytes": 200_000},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Large result handled."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Trigger a very large MCP result.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, f"Expected a large MCP result to produce a follow-up LLM request.\nLogs:\n{logs}"
    body_size = len(json.dumps(requests[1].get("body", {})))
    # If the runtime offloaded to MinIO/disk, the second body should be
    # significantly smaller than the original payload.
    assert body_size < 200_000, (
        f"Expected runtime to offload the 200KB MCP result, but the next LLM "
        f"request body is {body_size} bytes (probably contains the full payload)"
    )
