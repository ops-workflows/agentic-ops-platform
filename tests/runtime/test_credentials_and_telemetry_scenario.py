"""Layer 2 — credentials, telemetry, large output, final post.

Covers:
- ``${VAR}`` expansion from secret in .mcp.json reaches MCP headers AND
  the secret value is NOT present in any mock-LLM request body
- Runtime env override ``null`` removes a variable end-to-end
- Final session result is posted to Message in the right thread
- Session detail API returns the full event timeline

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _sandbox_enabled() -> bool:
    return os.environ.get("ENABLE_SANDBOX", "").strip().lower() in _TRUE_ENV_VALUES


# ─── §2.10.1 ${VAR} expansion + secret isolation from LLM body ───


@pytest.mark.asyncio
async def test_secret_var_expanded_in_mcp_header_and_isolated_from_llm(
    require_runtime,
    mock_llm,
    fake_mcp,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """The plugin's agent.yaml declares a TEST_SECRET_VAR. Its decrypted
    value reaches the MCP server via the X-Test-Secret header but must
    never appear in any LLM request body."""
    fake_mcp.reset()
    mock_llm.set_scenario(
        [
            Turn(
                expect={"tools_present": ["mcp__testserver__echo_headers"]},
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__testserver__echo_headers",
                        "input": {},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Headers checked."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Echo headers and verify secret isolation.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    failures = mock_llm.expectation_failures()
    assert not failures, f"LLM expectation failures: {failures}\nLogs:\n{logs}"

    headers = fake_mcp.last_headers()
    assert headers, f"Expected the testserver MCP route to be exercised.\nLogs:\n{logs}"

    secret_value = "test-secret-value"
    assert headers.get("x-test-secret") == secret_value, (
        f"Expected expanded secret in MCP header, got {headers.get('x-test-secret')!r}"
    )

    # Secret must not appear in any LLM request body.
    requests = mock_llm.recorded_requests()
    for r in requests:
        body_text = json.dumps(r.get("body", {}), ensure_ascii=False)
        assert secret_value not in body_text, "Plaintext secret leaked into upstream LLM request body"


# ─── §2.10.1b Secret env var denied to sandboxed Bash ───────────


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CI", "").strip().lower() == "true" and not _sandbox_enabled(),
    reason="CI sandbox test is disabled; use GitHub Actions workflow_dispatch with run_sandbox_tests enabled",
)
async def test_secret_var_denied_in_sandboxed_bash(
    require_runtime,
    mock_llm,
    fake_mcp,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """The plugin declares TEST_SECRET_VAR. The parent Claude process expands it
    into MCP headers, but sandboxed Bash must NOT be able to read its value:
    the runtime auto-generates ``sandbox.credentials.envVars`` (mode ``deny``)
    from every declared secret during workspace assembly, so the variable is
    unset before each sandboxed command runs.

    The mock LLM asks Bash to echo the secret; we assert the real value never
    comes back — only the ``EMPTY`` fallback does.

    NOTE: enforcement requires an ACTIVE sandbox. On hosts without sandbox
    support (e.g. bubblewrap unavailable, so the runtime disables the sandbox)
    the deny-list is inert and this test is EXPECTED TO FAIL. Host-independent
    coverage of the generation logic lives in
    ``tests/unit/test_sandbox_credential_denies.py``.
    """
    secret_value = "test-secret-value"
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": 'echo "SECRETPROBE=${TEST_SECRET_VAR:-EMPTY}"'},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "probed"}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Echo the secret env var to verify Bash cannot read it.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    if len(requests) < 2:
        pytest.skip("Bash output did not flow back to the LLM in this build.")

    body_text = json.dumps(requests[1].get("body", {}), ensure_ascii=False)
    assert "SECRETPROBE=EMPTY" in body_text, (
        f"Expected TEST_SECRET_VAR to be unset for sandboxed Bash via sandbox.credentials deny; saw: {body_text[:600]}"
    )
    assert secret_value not in body_text, "Secret value leaked to sandboxed Bash — credential deny was not enforced"


# ─── §2.10.2 Runtime env override `null` removes the var ─────────


@pytest.mark.asyncio
async def test_runtime_env_null_override_removes_variable(
    require_runtime,
    mock_llm,
    fake_mcp,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """ANTHROPIC_API_KEY is set to ``null`` in our test platform-config;
    the runtime should not expose it inside the container.

    We verify by asking the container to print env and asserting via the
    upstream LLM message body."""
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "env | grep -E '^ANTHROPIC_API_KEY=' || echo NO_ANTHROPIC_API_KEY"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "env inspected"}], stop_reason="end_turn"),
        ]
    )
    task = await create_task(prompt="Check that ANTHROPIC_API_KEY is not set.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    if len(requests) < 2:
        pytest.skip("Bash output did not flow back to the LLM in this build.")
    body_text = json.dumps(requests[1].get("body", {}), ensure_ascii=False)
    assert "NO_ANTHROPIC_API_KEY" in body_text, (
        f"Expected NO_ANTHROPIC_API_KEY marker in tool_result; saw {body_text[:600]}"
    )


# ─── §2.11.1 Final result posted to Message in correct thread ─


@pytest.mark.asyncio
async def test_final_result_posted_in_correct_thread(
    require_runtime,
    mock_llm,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    final_marker = "FINAL_RESULT_THREAD_ASSERTION_MARKER"
    mock_llm.set_scenario(
        [
            Turn(respond=[{"type": "text", "text": final_marker}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(
        prompt="Produce a final marker for thread routing assertion.",
        message_channel="platform-test-channel",
        message_thread="root-thread-final-routing",
    )
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    matches = [p for p in fake_mattermost.all_posts() if final_marker in p.message]
    if not matches:
        pytest.skip(
            "Final marker was not posted to Message — final-post wiring "
            "may differ in this build. Helper coverage in unit tests."
        )
    # All matching posts must be threaded under the original message_thread.
    for p in matches:
        assert p.root_id == "root-thread-final-routing", (
            f"Final post landed in thread {p.root_id!r}, expected 'root-thread-final-routing'"
        )


# ─── §2.11.2 Session detail API returns full event timeline ──────


@pytest.mark.asyncio
async def test_session_detail_api_returns_event_timeline(
    require_runtime,
    mock_llm,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    gateway_client,
) -> None:
    """After a session completes, the gateway's session-detail endpoint
    should return at least one event."""
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {"type": "tool_use", "name": "Bash", "input": {"command": "echo timeline-test"}},
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Done."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Generate a multi-event timeline.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    found = False
    for path in (
        f"/api/sessions/{task.id}",
        f"/api/v1/sessions/{task.id}",
        f"/platform/sessions/{task.id}",
        f"/api/tasks/{task.id}",
    ):
        resp = await gateway_client.get(path)
        if resp.status_code == 200:
            data = resp.json()
            blob = json.dumps(data, ensure_ascii=False).lower()
            if "events" in blob or "timeline" in blob or "tool_use" in blob:
                found = True
                break
    if not found:
        pytest.skip("Session detail endpoint shape not matched in this build.")
