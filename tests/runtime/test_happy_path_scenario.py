"""Layer 2 — happy-path end-to-end scenario.

Boots all fakes, spawns a real Docker container running the session
entrypoint against the mock LLM, and asserts on:
- container exits 0
- events are recorded by the event collector
- mock LLM received the expected number of requests
- the task prompt reached the LLM

Requires:
- Docker daemon running
- ``ai-ops-agent-runtime:latest`` image built
- ``TEST_RUNTIME_ENABLED=1`` and ``TEST_DATABASE_URL`` set
- Postgres reachable
"""

from __future__ import annotations

import pytest

from tests.fakes.mock_llm import Turn
from tests.runtime.scenarios import Scenario

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_runtime_happy_path(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
) -> None:
    """Container runs a single LLM turn and exits cleanly."""

    # Scenario: one text-only turn, no tool use, LLM says "done".
    scenario = Scenario(
        name="happy-path",
        prompt="Say hello and confirm the platform test works.",
        llm_turns=[
            Turn(
                respond=[{"type": "text", "text": "Hello! The platform test works correctly."}],
                stop_reason="end_turn",
            ),
        ],
        expected_task_status="succeeded",
    )

    mock_llm.set_scenario(scenario.llm_turns)

    task = await create_task(prompt=scenario.prompt)
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)

    # ── Container assertions ─────────────────────────────────
    assert exit_code == 0, f"Container exited with code {exit_code}.\nLogs:\n{logs}"

    # ── LLM assertions ───────────────────────────────────────
    requests = mock_llm.recorded_requests()
    assert len(requests) >= 1, "Expected at least one LLM request"

    # The task prompt should appear in the messages
    first_req = requests[0]
    messages = first_req.get("body", {}).get("messages", [])
    all_text = " ".join(
        str(block.get("text", ""))
        for m in messages
        for block in (m.get("content") if isinstance(m.get("content"), list) else [m])
        if isinstance(block, dict)
    )
    assert "platform test" in all_text.lower(), f"Expected task prompt in LLM messages, got: {all_text[:500]}"

    # ── Event collector assertions ───────────────────────────
    # The runtime should have posted at least a heartbeat and session events.
    assert len(collected_events) >= 1, f"Expected events from runtime, got {len(collected_events)}"

    # No soft expectation failures in the mock LLM
    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}"


@pytest.mark.asyncio
async def test_runtime_tool_use_and_end(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
) -> None:
    """Container handles one tool_use turn followed by end_turn."""

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {"type": "text", "text": "Let me check something."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo hello-from-test"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Done. The command succeeded."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Run a test echo command and report success.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)

    assert exit_code == 0, f"Container exited with code {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, f"Expected at least 2 LLM requests (tool_use + end), got {len(requests)}"
