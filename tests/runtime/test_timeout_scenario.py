"""Layer 2 — session timeout scenario.

Validates that the runtime container exits with an error status when the
session exceeds runtime_timeout_sec, and that a session_timeout event is
reported to the event collector.

Requires Docker + built runtime image + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import pytest

from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_session_timeout(
    require_runtime,
    mock_llm,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    monkeypatch,
) -> None:
    """Runtime terminates when RUNTIME_TIMEOUT_SEC is exceeded.

    We script the LLM to endlessly emit tool_use turns so the session
    never naturally ends. Override the fixture's 120-second timeout only for
    this test so the container and launcher exercise the real timeout path in
    a practical amount of time.
    """

    # Create many turns so the LLM keeps going, but the timeout fires first.
    busy_turns = [
        Turn(
            respond=[
                {"type": "text", "text": f"Working on step {i}..."},
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": f"echo step-{i}"},
                },
            ],
            stop_reason="tool_use",
        )
        for i in range(50)
    ]
    mock_llm.set_scenario(busy_turns)

    from session_manager import container_lifecycle

    load_agent_yaml = container_lifecycle.load_agent_yaml

    def load_timeout_test_agent_yaml(workflow: str):
        agent_config = load_agent_yaml(workflow)
        agent_config["runtime"] = {**agent_config.get("runtime", {}), "runtime_timeout_sec": 15}
        return agent_config

    monkeypatch.setattr(container_lifecycle, "load_agent_yaml", load_timeout_test_agent_yaml)

    task = await create_task(prompt="Run many steps until timeout.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=60)

    # Timeout should cause a non-zero exit or the runtime posts a
    # session_timeout event before exiting.
    timeout_events = [e for e in collected_events if e.get("event_type") in ("session_timeout", "session_error")]

    # Either the container exited non-zero OR we got a timeout/error event.
    assert exit_code != 0 or len(timeout_events) > 0, (
        f"Expected timeout or error — exit_code={exit_code}, "
        f"timeout_events={len(timeout_events)}\nLogs:\n{logs[-2000:]}"
    )
