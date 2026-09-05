"""Unit tests for trace_tree builder."""

from __future__ import annotations

import json

from shared.lib.trace_tree import SessionTraceResponse, build_trace_tree


def test_build_trace_tree_basic():
    events = [
        {
            "id": "ev-1",
            "event_type": "session_start",
            "timestamp": "2026-09-05T12:00:00Z",
            "data": {"agent": "test-coordinator", "prompt_preview": "Analyze alert"},
        },
        {
            "id": "ev-2",
            "event_type": "conversation_batch",
            "timestamp": "2026-09-05T12:00:01Z",
            "data": {
                "messages": [
                    {
                        "type": "assistant",
                        "timestamp": 1788619201.0,
                        "content": [
                            {"type": "text", "text": "Starting investigation"},
                            {"type": "thinking", "preview": "Reasoning about alert"},
                            {
                                "id": "tool-1",
                                "type": "tool_use",
                                "name": "Skill",
                                "input_preview": json.dumps({"skill": "splunk-queries"}),
                            },
                        ],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content_preview": "Skill loaded",
                        "is_error": False,
                    },
                ]
            },
        },
        {
            "id": "ev-3",
            "event_type": "session_complete",
            "timestamp": "2026-09-05T12:00:05Z",
            "data": {
                "result": "Root cause identified.",
                "input_tokens": 1200,
                "output_tokens": 450,
            },
        },
    ]

    trace = build_trace_tree(events, full_prompt="Full prompt here")
    assert isinstance(trace, SessionTraceResponse)
    assert trace.root.label == "test-coordinator"
    assert len(trace.root.children) == 5  # REQUEST, assistant text, thinking, tool_call, Final Result
    # Request node
    assert trace.root.children[0].badge == "REQUEST"
    assert trace.root.children[0].body == "Full prompt here"
    # Assistant text
    assert trace.root.children[1].kind == "assistant"
    assert trace.root.children[1].body == "Starting investigation"
    # Thinking node
    assert trace.root.children[2].kind == "thinking"
    assert trace.root.children[2].body == "Reasoning about alert"
    # Tool call
    assert trace.root.children[3].kind == "tool_call"
    assert trace.root.children[3].badge == "SKILL"
    assert trace.root.children[3].label == "splunk-queries"
    assert len(trace.root.children[3].children) == 1
    assert trace.root.children[3].children[0].kind == "tool_result"
    assert trace.root.children[3].children[0].body == "Skill loaded"
    # Final result
    assert trace.root.children[4].kind == "result"
    assert trace.root.children[4].label == "Final"
    assert trace.root.children[4].body == "Root cause identified."

    # Stats
    assert trace.stats.tool_calls == 1
    assert trace.stats.assistant_messages == 1
    assert trace.stats.tokens_in == 1200
    assert trace.stats.tokens_out == 450
    assert "splunk-queries" in trace.skills_used


def test_build_trace_tree_subagents_and_hooks():
    events = [
        {
            "id": "ev-1",
            "event_type": "session_start",
            "timestamp": "2026-09-05T12:00:00Z",
            "data": {"agent": "coord", "prompt_preview": "Triage"},
        },
        {
            "id": "ev-h1",
            "event_type": "hook_event",
            "timestamp": "2026-09-05T12:00:01Z",
            "data": {"hook_name": "auto_recall", "status": "success", "detail": "Recalled 2 incidents"},
        },
        {
            "id": "ev-2",
            "event_type": "conversation_batch",
            "timestamp": "2026-09-05T12:00:02Z",
            "data": {
                "messages": [
                    {
                        "type": "assistant",
                        "timestamp": 1788619202.0,
                        "content": [
                            {
                                "id": "agent-tool-1",
                                "type": "tool_use",
                                "name": "Agent",
                                "input_preview": json.dumps(
                                    {"subagent_type": "log-analyzer", "description": "search logs"}
                                ),
                            }
                        ],
                    },
                    {
                        "type": "system",
                        "subtype": "task_notification",
                        "data": {
                            "tool_use_id": "agent-tool-1",
                            "status": "completed",
                            "summary": "Log search found 0 errors.",
                        },
                    },
                ]
            },
        },
    ]

    trace = build_trace_tree(events)
    assert trace.stats.subagent_spawns == 1
    subagent_node = next(c for c in trace.root.children if c.kind == "subagent")
    assert subagent_node.label == "log-analyzer"
    assert len(subagent_node.children) == 1
    assert subagent_node.children[0].label == "Branch findings"
    assert subagent_node.children[0].body == "Log search found 0 errors."
