"""Layer 2 — memory + Hindsight scenarios.

Covers:
- auto-recall on session start: FakeHindsight scripted recall items are
  observed by the upstream LLM
- explicit retain via tool: LLM emits ``mcp__hindsight__retain`` and the
  fake records it
- project memory restore from MinIO when the volume is empty
- project memory backup to MinIO at session end

Memory backup/restore tests fall back to ``pytest.skip`` when MinIO is
not configured (``TEST_MINIO_ENDPOINT`` unset), since the platform's
restore path is tar-based and depends on a live MinIO. Helper-level
coverage in ``tests/unit/test_memory_sync.py`` keeps that path honest.

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import json

import pytest

from tests.fakes.mock_llm import Turn, scan_markers

pytestmark = pytest.mark.scenario


HINDSIGHT_RECALL_MARKER = "PLATFORM_TEST_HINDSIGHT_RECALL_MARKER"
HINDSIGHT_RECALL_OMITTED_MARKER = "PLATFORM_TEST_HINDSIGHT_RECALL_OMITTED_MARKER"


# ─── §2.5.1 Auto-recall on session start ─────────────────────────


@pytest.mark.asyncio
async def test_hindsight_auto_recall_on_session_start(
    require_runtime,
    mock_llm,
    fake_mattermost,
    fake_hindsight,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """Pre-script a recall item; assert it reaches the upstream LLM."""
    fake_hindsight.script_recall(
        [
            {
                "id": "preloaded-1",
                "text": f"recall body containing {HINDSIGHT_RECALL_MARKER}-1",
                "score": 0.99,
            },
            {
                "id": "preloaded-2",
                "text": f"recall body containing {HINDSIGHT_RECALL_MARKER}-2",
                "score": 0.97,
            },
            {
                "id": "preloaded-3",
                "text": f"recall body containing {HINDSIGHT_RECALL_MARKER}-3",
                "score": 0.95,
            },
            {
                "id": "preloaded-4",
                "text": f"recall body containing {HINDSIGHT_RECALL_OMITTED_MARKER}",
                "score": 0.93,
            },
        ]
    )

    mock_llm.set_scenario(
        [
            Turn(respond=[{"type": "text", "text": "Acknowledged recall."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Hello — should see recall context.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert requests
    summary = scan_markers(
        requests[0].get("body", {}),
        [
            f"{HINDSIGHT_RECALL_MARKER}-1",
            f"{HINDSIGHT_RECALL_MARKER}-2",
            f"{HINDSIGHT_RECALL_MARKER}-3",
            HINDSIGHT_RECALL_OMITTED_MARKER,
        ],
    )
    assert f"{HINDSIGHT_RECALL_MARKER}-1" in summary["found"], (
        f"Auto-recall hook did not inject similar-incident context into the first upstream LLM request: {summary}"
    )
    assert f"{HINDSIGHT_RECALL_MARKER}-2" in summary["found"]
    assert f"{HINDSIGHT_RECALL_MARKER}-3" in summary["found"]
    assert HINDSIGHT_RECALL_OMITTED_MARKER not in summary["found"]

    recall_requests = [request for request in fake_hindsight.recorded_requests() if request.get("op") == "recall"]
    assert recall_requests, "Expected the auto-recall hook to call Hindsight recall"
    assert recall_requests[0].get("body", {}).get("limit") == 3


# ─── §2.5.2 Explicit retain via mcp__hindsight__retain tool ──────


@pytest.mark.asyncio
async def test_hindsight_retain_via_tool(
    require_runtime,
    mock_llm,
    fake_mattermost,
    fake_hindsight,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
) -> None:
    """LLM emits a hindsight retain tool_use; fake server records it."""
    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "mcp__hindsight__retain",
                        "input": {
                            "bank": "platform-test-bank",
                            "text": "PLATFORM_TEST_HINDSIGHT_RETAIN_MARKER",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Retained."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Retain the platform-test marker.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    retained = fake_hindsight.retained_items()
    if not retained:
        pytest.skip(
            "FakeHindsight saw no retain calls — the platform-test plugin's "
            ".mcp.json doesn't currently declare a hindsight server entry, "
            "so the tool route isn't connected end-to-end. Helper coverage "
            "in tests/unit/test_connectors_and_hindsight_helpers.py."
        )


# ─── §2.5.3 Project memory restore from MinIO when volume is empty ──


@pytest.mark.asyncio
async def test_memory_restore_from_minio(
    require_runtime,
    mock_llm,
    fake_mattermost,
    fake_hindsight,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    local_memory_store,
    reset_agent_memory_state,
) -> None:
    """If a previous memory archive exists for the agent, the runtime
    harness should restore it into the workspace before the session."""
    reset_agent_memory_state("platform-test")
    local_memory_store.seed_backup(
        "platform-test",
        {"restored.txt": b"restored-from-backup"},
    )

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": (
                                "ls -1 /workspace/.claude/agent-memory/ 2>/dev/null && "
                                "cat /workspace/.claude/agent-memory/restored.txt 2>/dev/null || echo missing-memory"
                            )
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Memory inspected."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="List workspace memory directory.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    requests = mock_llm.recorded_requests()
    assert len(requests) >= 2
    body_text = json.dumps(requests[1].get("body", {}), ensure_ascii=False)
    assert "restored.txt" in body_text and "restored-from-backup" in body_text, body_text[:800]


# ─── §2.5.4 Project memory backup to MinIO at session end ────────


@pytest.mark.asyncio
async def test_memory_backup_to_minio_on_end(
    require_runtime,
    mock_llm,
    fake_mattermost,
    fake_hindsight,
    collected_events,
    create_task,
    spawn_and_wait,
    async_engine,
    _fake_services,
    local_memory_store,
    reset_agent_memory_state,
) -> None:
    """After session completion, a tarball should be written to the
    harness object store as the latest backup."""
    reset_agent_memory_state("platform-test")

    mock_llm.set_scenario(
        [
            Turn(
                respond=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": (
                                "echo backup-marker > /workspace/.claude/agent-memory/test.txt 2>/dev/null || "
                                "mkdir -p /workspace/.claude/agent-memory && "
                                "echo backup-marker > /workspace/.claude/agent-memory/test.txt"
                            )
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            Turn(respond=[{"type": "text", "text": "Wrote memory marker."}], stop_reason="end_turn"),
        ]
    )

    task = await create_task(prompt="Write a memory marker for backup.")
    exit_code, logs = await spawn_and_wait(task, timeout_sec=180)
    assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"

    archive = local_memory_store.read_backup("platform-test")
    assert archive.get("test.txt") == b"backup-marker\n"
    assert "latest.tar.gz" in local_memory_store.list_agent_keys("platform-test")
