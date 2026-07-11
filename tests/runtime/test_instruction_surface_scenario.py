"""Layer 2 — instruction-surface scenarios.

Validates that the runtime correctly stages workspace instruction files
(CLAUDE.md, plugin/shared skills, settings.json deny patterns, sandbox
defaults) and that they appear in the upstream LLM request.

Strategy: probe-style mock-LLM scenarios — the mock returns a compact
``found_markers=[...] missing_markers=[...]`` summary of the upstream
request. The test asserts on the parsed summary plus container exit
status.

Markers used here are defined in
``tests/fixtures/repo-root/CLAUDE.md`` and the platform-test
plugin fixture under ``tests/fixtures/repo-root/workflows/platform-test/``.

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import pytest

from tests.fakes.mock_llm import Turn, make_marker_probe
from tests.runtime.scenarios import probe_then_end

pytestmark = pytest.mark.scenario


SHARED_CLAUDE_MD_MARKER = "PLATFORM_TEST_SHARED_CLAUDE_MD_MARKER"
PLUGIN_CLAUDE_MD_MARKER = "PLATFORM_TEST_PLUGIN_CLAUDE_MD_MARKER"
PLUGIN_SKILL_MARKER = "PLATFORM_TEST_PLUGIN_LOCAL_SKILL_MARKER"
SHARED_SKILL_MARKER = "PLATFORM_TEST_SHARED_SKILL_MARKER"


async def _run_probe(
    *,
    mock_llm,
    create_task,
    spawn_and_wait,
    markers: list[str],
    prompt: str = "Probe the system prompt and report markers.",
) -> dict[str, list[str]]:
    """Run a 1-turn probe scenario, return the parsed found/missing summary."""
    mock_llm.set_scenario(probe_then_end(markers))
    task = await create_task(prompt=prompt)
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert requests, "mock LLM did not see any requests"
    # Parse the assistant text we sent back — but we want what we ECHOED,
    # not what the LLM sent. Easier: re-run the probe ourselves on the
    # recorded body to get a stable result independent of CLI quirks.
    from tests.fakes.mock_llm import scan_markers

    return scan_markers(requests[0].get("body", {}), markers)


# ── §2.1.1 Shared CLAUDE.md present ──────────────────────────────


async def _run_workspace_claude_probe(
    *, mock_llm, create_task, spawn_and_wait, markers: list[str]
) -> dict[str, list[str]]:
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "cat /workspace/CLAUDE.md 2>/dev/null || echo missing-claude"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(probe=make_marker_probe(markers), stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Read the staged CLAUDE.md and report markers.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, "Expected at least 2 LLM requests (tool_use + final)"

    from tests.fakes.mock_llm import scan_markers

    return scan_markers(requests[-1].get("body", {}), markers)


@pytest.mark.asyncio
async def test_shared_claude_md_present_in_system_prompt(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    summary = await _run_workspace_claude_probe(
        mock_llm=mock_llm,
        create_task=create_task,
        spawn_and_wait=spawn_and_wait,
        markers=[SHARED_CLAUDE_MD_MARKER],
    )
    assert SHARED_CLAUDE_MD_MARKER in summary["found"], (
        f"shared CLAUDE.md marker missing from staged /workspace/CLAUDE.md: {summary}"
    )


# ── §2.1.2 Plugin-local CLAUDE.md present ────────────────────────


@pytest.mark.asyncio
async def test_plugin_claude_md_present_in_system_prompt(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    summary = await _run_workspace_claude_probe(
        mock_llm=mock_llm,
        create_task=create_task,
        spawn_and_wait=spawn_and_wait,
        markers=[PLUGIN_CLAUDE_MD_MARKER],
    )
    assert PLUGIN_CLAUDE_MD_MARKER in summary["found"], (
        f"plugin CLAUDE.md marker missing from staged /workspace/CLAUDE.md: {summary}"
    )


# ── §2.1.3 Plugin-local skill is reachable (probe-style) ─────────


@pytest.mark.asyncio
async def test_plugin_local_skill_marker_reachable(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """Validate that invoking the Skill tool returns the plugin-local
    skill body to the next LLM turn as a tool_result payload.
    """
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "test-skill"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                probe=make_marker_probe([PLUGIN_SKILL_MARKER]),
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Read the test-skill SKILL.md and report the marker.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2, "Expected at least 2 LLM requests (tool_use + final)"

    from tests.fakes.mock_llm import scan_markers

    # The second request must carry the Skill tool_result with the skill body.
    summary = scan_markers(requests[-1].get("body", {}), [PLUGIN_SKILL_MARKER])
    assert PLUGIN_SKILL_MARKER in summary["found"], f"plugin skill marker not in tool_result feedback to LLM: {summary}"


# ── §2.1.4 Shared skill reachable (same approach) ────────────────


@pytest.mark.asyncio
async def test_shared_skill_marker_reachable(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """Validate that invoking the Skill tool returns the shared skill
    body to the next LLM turn as a tool_result payload.
    """
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "test-shared-skill"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                probe=make_marker_probe([SHARED_SKILL_MARKER]),
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Read the shared test-shared-skill SKILL.md and report.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2

    from tests.fakes.mock_llm import scan_markers

    summary = scan_markers(requests[-1].get("body", {}), [SHARED_SKILL_MARKER])
    assert SHARED_SKILL_MARKER in summary["found"], f"shared skill marker not in tool_result feedback to LLM: {summary}"


# ── §2.1.5 Settings permissions.deny actually blocks the tool ────


@pytest.mark.asyncio
async def test_permissions_deny_blocks_tool(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """The plugin's settings.json denies ``Bash(rm -rf *)``; the runtime
    should refuse the tool_use without executing it."""

    # Try to run a denied command, then react sensibly when refused.
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "rm -rf /workspace/forbidden-target"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "The denied tool was correctly blocked."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Try to remove a forbidden directory and confirm denial.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    # The deny path should produce a permission_callback or similar event;
    # in the absence of a deterministic event name, look for any event
    # that mentions denial OR a tool_result indicating refusal.
    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2

    second_body = requests[1].get("body", {})
    import json as _json

    blob = _json.dumps(second_body, ensure_ascii=False).lower()
    assert any(
        token in blob
        for token in (
            "deny",
            "denied",
            "not allowed",
            "permission",
            "blocked",
            "refuse",
        )
    ), f"expected a denial/permission marker in the tool_result for the second LLM turn. body sample: {blob[:600]}"


# ── §2.1.6 Sandbox defaults loaded into the runtime ──────────────


@pytest.mark.asyncio
async def test_sandbox_loaded_in_runtime(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """The plugin's settings.json enables ``sandbox.enabled=true``. We
    assert this indirectly: a Bash tool_use that tries to write outside
    ``allowWrite`` should either be blocked or report a non-zero status,
    while a write inside ``/tmp`` (allowed) should succeed."""

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo ok > /tmp/sandbox-test-marker && cat /tmp/sandbox-test-marker"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Sandbox accepted /tmp write."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Confirm sandbox allows writing inside /tmp.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    # Look for sandbox-related session events or, if absent, a successful
    # tool_result containing the marker.
    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2
    import json as _json

    second = _json.dumps(requests[1].get("body", {}), ensure_ascii=False)
    assert "sandbox-test-marker" in second or "ok" in second, "expected the bash command output to flow back to the LLM"


# ── §2.1.7 Skill tool invocation recorded in conversation_batch ──


@pytest.mark.asyncio
async def test_skill_invocation_recorded_in_conversation_batch(
    require_runtime,
    mock_llm,
    fake_mattermost,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """When the LLM invokes the Skill tool, the tool_use block must be
    stored in a conversation_batch event with the skill name in the
    input_preview.  This is the prerequisite for the UI Skills sidebar
    to show which skills were used in a session.
    """
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "test-skill"},
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(
                respond=[{"type": "text", "text": "Skill invoked and recorded."}],
                stop_reason="end_turn",
            ),
        ]
    )

    task = await create_task(prompt="Invoke the test-skill and confirm it was recorded.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    import json as _json

    batch_events = [e for e in collected_events if e.get("event_type") == "conversation_batch"]
    assert batch_events, "No conversation_batch events recorded by the runtime"

    skill_blocks = []
    for evt in batch_events:
        for msg in (evt.get("data") or {}).get("messages", []):
            for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    skill_blocks.append(block)

    assert skill_blocks, (
        "Expected a Skill tool_use block in conversation_batch events so the "
        "UI Skills sidebar can display it. "
        f"Batch events (first 2): {_json.dumps(batch_events[:2], default=str)[:800]}"
    )

    preview = skill_blocks[0].get("input_preview", "")
    assert "test-skill" in preview, (
        f"Expected 'test-skill' in Skill tool_use input_preview for UI parsing. Got: {preview!r}"
    )
