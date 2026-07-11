#!/usr/bin/env python3
"""SubagentStop hook — auto-retain RCA findings in Hindsight.

Writes two memories on investigator completion:
- a business-facing RCA record for incident recall and digesting
- a workflow-learning trace for later weekly reflection and skill improvement

Hook response: continue=true (never blocks), no user-visible output.
"""

import json
import os
import re
import sys
from collections import Counter
from typing import Any

import httpx

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://hindsight:8888")
GATEWAY_EVENT_URL = os.environ.get("GATEWAY_EVENT_URL", "")
HINDSIGHT_BANKS_PREFIX = "/v1/default/banks"
MAX_RESULT_CHARS = 4000
MAX_LEARNING_CHARS = 5000
TRACE_TOOL_LIMIT = 12
TRACE_ERROR_LIMIT = 8
TRACE_NOTE_LIMIT = 6
TRACE_RESULT_LIMIT = 10
TOOL_INPUT_PREVIEW_LIMIT = 180
TOOL_RESULT_PREVIEW_LIMIT = 100


def emit_hook_event(*, status: str, detail: str = ""):
    task_id = os.environ.get("TASK_ID", "")
    if not GATEWAY_EVENT_URL or not task_id:
        print(f"[retain_hook] skipping emit: URL={GATEWAY_EVENT_URL!r} TASK_ID={task_id!r}", file=sys.stderr)
        return

    try:
        resp = httpx.post(
            GATEWAY_EVENT_URL,
            json={
                "task_id": task_id,
                "event_type": "hook_event",
                "data": {
                    "hook_name": "retain_incident",
                    "hook_event": "SubagentStop",
                    "status": status,
                    "detail": detail[:1000],
                },
            },
            timeout=2.5,
        )
        print(f"[retain_hook] emitted {status} -> {resp.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"[retain_hook] emit failed: {exc}", file=sys.stderr)


def _clip(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _clip_block(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _gateway_api_base() -> str:
    explicit = str(os.environ.get("GATEWAY_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if GATEWAY_EVENT_URL.endswith("/events"):
        return GATEWAY_EVENT_URL[: -len("/events")]
    return "http://gateway:8080"


def _catalog_bank_id(task_workflow: str, bank_kind: str) -> str | None:
    workflow = task_workflow.strip().lower()
    if not workflow:
        return None
    try:
        response = httpx.get(f"{_gateway_api_base()}/api/platform/memories", timeout=2.5)
        payload = response.json()
    except Exception as exc:
        print(f"[retain_hook] memory catalog lookup failed: {exc}", file=sys.stderr)
        return None

    for bank in payload.get("hindsight_banks") or []:
        if not isinstance(bank, dict):
            continue
        if bank.get("kind") != bank_kind:
            continue
        workflows = [str(item).strip().lower() for item in bank.get("workflows") or []]
        if workflow in workflows and bank.get("bank_id"):
            return str(bank["bank_id"])
    return None


def resolve_bank_id(task_workflow: str, bank_kind: str = "business") -> str:
    if bank_kind == "learning":
        override = str(os.environ.get("HINDSIGHT_LEARNING_BANK_ID") or "").strip()
        if override:
            return override
        return _catalog_bank_id(task_workflow, "learning") or "workflow-learning"
    override = str(os.environ.get("HINDSIGHT_BUSINESS_BANK_ID") or os.environ.get("HINDSIGHT_BANK_ID") or "").strip()
    if override:
        return override
    return _catalog_bank_id(task_workflow, "business") or "incident-rca"


def _extract_case_id(result_text: str, task_prompt: str) -> str:
    combined = "\n".join(part for part in [result_text, task_prompt] if part)
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "Incident Analysis:" in stripped:
            return stripped.split("Incident Analysis:", 1)[-1].strip().rstrip("#").strip()
        if stripped.lower().startswith(("incident:", "incident id:", "case:", "case id:")):
            return stripped.split(":", 1)[-1].strip()

    match = re.search(r"\b(?:INC|CASE|CS|SF)-?[A-Z0-9-]{3,}\b", combined, re.IGNORECASE)
    if match:
        return match.group(0)
    return "unknown"


def _issue_summary(task_prompt: str, case_id: str, result_text: str) -> str:
    prompt_summary = _clip(task_prompt, 500)
    if prompt_summary:
        return prompt_summary
    if case_id != "unknown":
        return f"Investigation for {case_id}"
    return _clip(result_text.splitlines()[0] if result_text else "Incident investigation", 500)


def _result_summary(result_text: str) -> str:
    for line in result_text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return _clip(stripped, 500)
    return "No final result captured"


def _fetch_session_detail(task_id: str) -> dict[str, Any] | None:
    if not task_id:
        return None

    try:
        response = httpx.get(f"{_gateway_api_base()}/api/sessions/{task_id}", timeout=6.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[retain_hook] session fetch failed: {exc}", file=sys.stderr)
        return None


def _build_session_trace(session_detail: dict[str, Any] | None, result_text: str) -> dict[str, Any]:
    task = (session_detail or {}).get("task") or {}
    prompt_text = str(task.get("prompt") or "")
    tool_calls: list[str] = []
    tool_errors: list[str] = []
    tool_results: list[str] = []
    assistant_notes: list[str] = []
    tool_names_by_id: dict[str, str] = {}
    tool_name_counts: Counter[str] = Counter()

    for event in (session_detail or {}).get("events") or []:
        if event.get("event_type") != "conversation_batch":
            continue
        data = event.get("data") or {}
        for message in data.get("messages") or []:
            msg_type = message.get("type")
            if msg_type == "assistant":
                for block in message.get("content") or []:
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        tool_name = str(block.get("name") or "unknown_tool")
                        tool_id = str(block.get("id") or "")
                        if tool_id:
                            tool_names_by_id[tool_id] = tool_name
                        tool_name_counts[tool_name] += 1
                        preview = _clip(str(block.get("input_preview") or ""), TOOL_INPUT_PREVIEW_LIMIT)
                        tool_calls.append(f"{tool_name}: {preview}" if preview else tool_name)
                    elif block_type == "text":
                        note = _clip(str(block.get("text") or ""), 220)
                        if note:
                            assistant_notes.append(note)
            elif msg_type == "tool_result":
                tool_name = tool_names_by_id.get(str(message.get("tool_use_id") or ""), "unknown_tool")
                preview = _clip(str(message.get("content_preview") or ""), TOOL_RESULT_PREVIEW_LIMIT)
                if message.get("is_error"):
                    tool_errors.append(f"{tool_name}: {preview}" if preview else tool_name)
                else:
                    tool_results.append(f"{tool_name}: {preview}" if preview else tool_name)

    repeated_tools = [f"{name} x{count}" for name, count in tool_name_counts.items() if count > 1]
    successful_tools: list[str] = []
    failed_tool_names = {item.split(":", 1)[0] for item in tool_errors}
    for item in tool_calls:
        tool_name = item.split(":", 1)[0]
        if tool_name not in failed_tool_names and tool_name not in successful_tools:
            successful_tools.append(tool_name)

    return {
        "task_prompt": prompt_text,
        "tool_calls": tool_calls[:TRACE_TOOL_LIMIT],
        "tool_errors": tool_errors[:TRACE_ERROR_LIMIT],
        "tool_results": tool_results[:TRACE_RESULT_LIMIT],
        "repeated_tools": repeated_tools[:TRACE_TOOL_LIMIT],
        "successful_tools": successful_tools[:TRACE_TOOL_LIMIT],
        "assistant_notes": assistant_notes[:TRACE_NOTE_LIMIT],
        "result_summary": _result_summary(result_text),
    }


def _business_content(issue: str, result_text: str) -> str:
    return _clip_block(
        "\n".join(
            [
                f"Issue: {issue}",
                "",
                "Result:",
                result_text.strip(),
            ]
        ),
        MAX_RESULT_CHARS,
    )


def _learning_content(issue: str, trace: dict[str, Any], result_text: str) -> str:
    repeated_tool_usage = "; ".join(trace.get("repeated_tools") or []) or "No repeated loops captured"
    failed_tool_usage = "; ".join(trace.get("tool_errors") or []) or "No clear failed branch was captured"
    return _clip_block(
        "\n".join(
            [
                f"Issue: {issue or 'Unknown issue'}",
                f"Final result: {trace.get('result_summary') or _result_summary(result_text)}",
                f"Task prompt: {_clip(trace.get('task_prompt') or '', 500)}",
                f"Tool attempts: {_clip('; '.join(trace.get('tool_calls') or []) or 'Not captured', 700)}",
                f"Useful tool results: {_clip('; '.join(trace.get('tool_results') or []) or 'Not captured', 700)}",
                f"Repeated tool usage: {_clip(repeated_tool_usage, 500)}",
                f"What worked: {_clip('; '.join(trace.get('successful_tools') or []) or 'Not captured', 320)}",
                f"What did not work: {_clip(failed_tool_usage, 500)}",
                f"Assistant milestones: {_clip('; '.join(trace.get('assistant_notes') or []) or 'Not captured', 600)}",
            ]
        ),
        MAX_LEARNING_CHARS,
    )


def _retain_memory(*, bank_id: str, content: str, context: str, document_id: str, metadata: dict[str, Any]) -> str:
    response = httpx.post(
        f"{HINDSIGHT_URL}{HINDSIGHT_BANKS_PREFIX}/{bank_id}/memories",
        json={
            "async": True,
            "items": [
                {
                    "content": content,
                    "context": context,
                    "document_id": document_id,
                    "metadata": {str(key): str(value) for key, value in metadata.items() if value is not None},
                }
            ],
        },
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("operation_id") or "").strip()


def main():
    """Read SubagentStop event from stdin, retain in Hindsight."""
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        print(json.dumps({"continue": True}))
        return

    agent_type = event.get("agent_type", "")
    result_text = str(event.get("last_assistant_message") or event.get("last_result_text") or "").strip()
    if not result_text:
        emit_hook_event(status="skipped", detail="No final assistant message to retain")
        print(json.dumps({"continue": True}))
        return

    task_id = os.environ.get("TASK_ID", "")
    task_workflow = os.environ.get("TASK_WORKFLOW", "")
    session_detail = _fetch_session_detail(task_id)
    task_prompt = str(((session_detail or {}).get("task") or {}).get("prompt") or "")
    case_id = _extract_case_id(result_text, task_prompt)
    issue = _issue_summary(task_prompt, case_id, result_text)
    trace = _build_session_trace(session_detail, result_text)

    business_bank_id = resolve_bank_id(task_workflow, "business")
    learning_bank_id = resolve_bank_id(task_workflow, "learning")

    try:
        business_operation_id = _retain_memory(
            bank_id=business_bank_id,
            content=_business_content(issue, result_text),
            context=f"Incident RCA for {case_id}",
            document_id=task_id or case_id,
            metadata={
                "case_id": case_id,
                "task_id": task_id,
                "type": "rca_analysis",
                "workflow": task_workflow,
                "bank_kind": "business",
                "issue": issue,
            },
        )
        learning_operation_id = _retain_memory(
            bank_id=learning_bank_id,
            content=_learning_content(issue, trace, result_text),
            context=f"Workflow learning trace for {case_id}",
            document_id=(task_id or case_id) + ":learning",
            metadata={
                "case_id": case_id,
                "task_id": task_id,
                "type": "workflow_learning_trace",
                "workflow": task_workflow,
                "bank_kind": "learning",
                "issue": issue,
                "result": _result_summary(result_text),
                "repeated_tools": "; ".join(trace.get("repeated_tools") or []),
                "successful_tools": "; ".join(trace.get("successful_tools") or []),
                "tool_error_count": len(trace.get("tool_errors") or []),
            },
        )
        emit_hook_event(
            status="success",
            detail=(
                f"Queued RCA retain for {case_id}"
                + (f" business_operation_id={business_operation_id}" if business_operation_id else "")
                + (f" learning_operation_id={learning_operation_id}" if learning_operation_id else "")
                + (f" from agent_type={agent_type}" if agent_type else "")
            ),
        )
        print(f"Queued RCA retain for {case_id}", file=sys.stderr)
    except Exception as exc:
        emit_hook_event(status="error", detail=str(exc))
        print(f"[retain_hook] Failed to retain: {exc}", file=sys.stderr)

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
