"""Layer 2 — MCP header expansion scenario.

Validates that env vars (TASK_ID, TASK_WORKFLOW, MESSAGE_CHANNEL, agent env,
agent secrets) are correctly expanded into MCP server request headers
when the runtime calls the testserver MCP tool.

Requires Docker + built runtime image + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import pytest

from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_mcp_header_expansion(
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
    """MCP headers carry expanded env vars from the session context."""
    fake_mcp.reset()

    # Turn 1: LLM calls the echo_headers MCP tool
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__testserver__echo_headers"]},
                respond=[
                    {"type": "text", "text": "Let me check the MCP headers."},
                    {
                        "type": "tool_use",
                        "name": "mcp__testserver__echo_headers",
                        "input": {},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Headers verified successfully."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(
        prompt="Call the echo_headers MCP tool and report what headers you see.",
        message_channel="platform-test-channel",
    )

    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)

    assert exit_code == 0, f"Container exited with code {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    headers = fake_mcp.last_headers()
    assert headers, f"Expected the testserver MCP route to be exercised.\nLogs:\n{logs}"
    assert headers.get("x-task-id") == str(task.id), f"Expected X-Task-Id={task.id}, got {headers.get('x-task-id')}"
    assert headers.get("x-task-workflow") == "platform-test", (
        f"Expected X-Task-Workflow=platform-test, got {headers.get('x-task-workflow')}"
    )
    assert headers.get("x-test-fixed") == "test-fixed-value", (
        f"Expected X-Test-Fixed=test-fixed-value, got {headers.get('x-test-fixed')}"
    )
