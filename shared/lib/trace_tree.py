"""Trace Tree Builder.

Constructs a canonical hierarchical trace tree and session stats from raw
session events. Ports the control-plane UI buildTree() logic to Python so the
backend can serve pre-parsed traces directly with 100% parity.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class BaseCamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class TraceNodeResponse(BaseCamelModel):
    id: str
    kind: str
    timestamp: str
    label: str
    detail: str | None = None
    body: str | None = None
    duration: float | None = None
    is_error: bool = False
    badge: str | None = None
    children: list[TraceNodeResponse] = Field(default_factory=list)
    raw: Any = None
    meta: dict[str, str] = Field(default_factory=dict)


class SessionStatsResponse(BaseCamelModel):
    tool_calls: int = 0
    tool_errors: int = 0
    assistant_messages: int = 0
    subagent_spawns: int = 0
    total_turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class SessionTraceResponse(BaseCamelModel):
    root: TraceNodeResponse
    stats: SessionStatsResponse
    heartbeats: list[dict[str, Any]] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)
    mcps_used: list[str] = Field(default_factory=list)
    event_count: int = 0


# ─── Helper Functions ────────────────────────────────────────────────────────


def _format_trace_body(input_val: str) -> str:
    try:
        parsed = json.loads(input_val)
        return json.dumps(parsed, indent=2)
    except Exception:
        return input_val


def _epoch_to_iso(v: Any, fallback: str) -> str:
    if isinstance(v, (int, float)):
        try:
            dt = datetime.fromtimestamp(v, tz=UTC)
            ms = dt.microsecond // 1000
            return dt.strftime(f"%Y-%m-%dT%H:%M:%S.{ms:03d}Z")
        except Exception:
            return fallback
    return fallback


def _flat_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
    return "\n\n".join(texts).strip()


def _parse_agent_preview(input_str: str) -> tuple[str, str | None]:
    name = "subagent"
    detail: str | None = None
    with contextlib.suppress(Exception):
        parsed = json.loads(input_str)
        if isinstance(parsed, dict):
            if isinstance(parsed.get("subagent_type"), str):
                name = parsed["subagent_type"]
            elif isinstance(parsed.get("description"), str):
                name = parsed["description"]
            if isinstance(parsed.get("description"), str):
                detail = parsed["description"]
            return name, detail

    subagent_match = re.search(r'"subagent_type"\s*:\s*"([^"]+)"', input_str)
    desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', input_str)
    if subagent_match:
        name = subagent_match.group(1)
    elif desc_match:
        name = desc_match.group(1)
    if desc_match:
        detail = desc_match.group(1)
    return name, detail


def _parse_skill_preview(input_str: str) -> str | None:
    with contextlib.suppress(Exception):
        parsed = json.loads(input_str)
        if isinstance(parsed, dict) and isinstance(parsed.get("skill"), str):
            return parsed["skill"]
    match = re.search(r'"skill"\s*:\s*"([^"]+)"', input_str)
    return match.group(1) if match else None


def _parse_send_message_preview(input_str: str) -> tuple[str | None, str | None, str | None]:
    with contextlib.suppress(Exception):
        parsed = json.loads(input_str)
        if isinstance(parsed, dict):
            recipient_id = (
                parsed.get("to")
                if isinstance(parsed.get("to"), str)
                else (parsed.get("recipient") if isinstance(parsed.get("recipient"), str) else None)
            )
            message = (
                parsed.get("message")
                if isinstance(parsed.get("message"), str)
                else (parsed.get("content") if isinstance(parsed.get("content"), str) else None)
            )
            summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else None
            return recipient_id, message, summary

    to_m = re.search(r'"(?:to|recipient)"\s*:\s*"([^"]+)"', input_str)
    msg_m = re.search(r'"(?:message|content)"\s*:\s*"([^"]+)"', input_str)
    sum_m = re.search(r'"summary"\s*:\s*"([^"]+)"', input_str)
    return (
        to_m.group(1) if to_m else None,
        msg_m.group(1) if msg_m else None,
        sum_m.group(1) if sum_m else None,
    )


def _summarize_task_type(val: Any) -> str | None:
    if not isinstance(val, str) or not val.strip():
        return None
    return val.replace("_", " ")


def _is_duplicate_narrative(left: str | None, right: str | None) -> bool:
    normalized_left = left.strip() if left else ""
    normalized_right = right.strip() if right else ""
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    prefix_length = min(len(normalized_left), len(normalized_right), 280)
    return normalized_left[:prefix_length] == normalized_right[:prefix_length]


# ─── Tree Builder ────────────────────────────────────────────────────────────


def build_trace_tree(events: list[dict[str, Any]], full_prompt: str | None = None) -> SessionTraceResponse:
    first_ts = events[0].get("timestamp") if events else datetime.now(UTC).isoformat()
    if not isinstance(first_ts, str):
        first_ts = datetime.now(UTC).isoformat()

    root = TraceNodeResponse(
        id="root",
        kind="session",
        timestamp=first_ts,
        label="Trace",
        children=[],
    )

    heartbeats: list[dict[str, Any]] = []
    tool_name_map: dict[str, str] = {}
    pending_tool_calls: dict[str, TraceNodeResponse] = {}
    message_tool_ids: set[str] = set()
    subagents_by_task_id: dict[str, TraceNodeResponse] = {}
    subagent_nodes_by_tool_id: dict[str, TraceNodeResponse] = {}
    subagent_names_by_agent_id: dict[str, str] = {}
    result_nodes: list[TraceNodeResponse] = []
    skill_names: set[str] = set()
    mcp_servers: set[str] = set()
    default_agent: str | None = None

    active_subagent: TraceNodeResponse | None = None
    stats = SessionStatsResponse()

    def current_parent() -> TraceNodeResponse:
        return active_subagent if active_subagent is not None else root

    def resolve_msg_parent(msg: dict[str, Any] | None) -> TraceNodeResponse:
        pid = (
            msg.get("parent_tool_use_id")
            if isinstance(msg, dict) and isinstance(msg.get("parent_tool_use_id"), str)
            else None
        )
        if pid:
            node = subagent_nodes_by_tool_id.get(pid)
            if node:
                return node
        return root

    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        event_type = event.get("event_type", "")
        event_id = str(event.get("id", ""))
        event_ts = str(event.get("timestamp", ""))

        if event_type == "heartbeat":
            heartbeats.append(event)
            continue

        if event_type == "session_start":
            if isinstance(data.get("agent"), str) and data["agent"].strip():
                default_agent = data["agent"].strip()
                root.label = default_agent

            prompt_body = (
                full_prompt.strip()
                if full_prompt and full_prompt.strip()
                else (data.get("prompt_preview") if isinstance(data.get("prompt_preview"), str) else None)
            )
            root.timestamp = event_ts
            root.detail = prompt_body
            root.children.append(
                TraceNodeResponse(
                    id=event_id,
                    kind="lifecycle",
                    timestamp=event_ts,
                    label="",
                    detail="",
                    body=prompt_body,
                    badge="REQUEST",
                    children=[],
                    raw=data,
                )
            )
            continue

        if event_type == "session_phase":
            phase = data.get("phase") if isinstance(data.get("phase"), str) else "unknown"
            if phase in {"first_sdk_message", "claude_query_complete", "claude_query_start"}:
                continue
            current_parent().children.append(
                TraceNodeResponse(
                    id=event_id,
                    kind="lifecycle",
                    timestamp=event_ts,
                    label=phase,
                    children=[],
                    raw=data,
                )
            )
            continue

        if event_type == "conversation_batch":
            messages = data.get("messages") if isinstance(data.get("messages"), list) else []
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                ts = _epoch_to_iso(msg.get("timestamp"), event_ts)
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                msg_type = msg.get("type")

                # User message
                if msg_type == "user":
                    text = _flat_text(content)
                    if text:
                        resolve_msg_parent(msg).children.append(
                            TraceNodeResponse(
                                id=f"{event_id}-u-{i}",
                                kind="user",
                                timestamp=ts,
                                label="User",
                                body=text,
                                children=[],
                                raw=msg,
                            )
                        )
                    continue

                # Assistant message
                if msg_type == "assistant":
                    text = _flat_text(content)
                    thinking_parts = [
                        b["preview"]
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "thinking" and isinstance(b.get("preview"), str)
                    ]
                    thinking = "\n\n".join(thinking_parts).strip()
                    tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]

                    if text:
                        stats.assistant_messages += 1
                        resolve_msg_parent(msg).children.append(
                            TraceNodeResponse(
                                id=f"{event_id}-a-{i}",
                                kind="assistant",
                                timestamp=ts,
                                label="",
                                body=text,
                                children=[],
                                raw=msg,
                            )
                        )

                    if thinking:
                        resolve_msg_parent(msg).children.append(
                            TraceNodeResponse(
                                id=f"{event_id}-think-{i}",
                                kind="thinking",
                                timestamp=ts,
                                label="Reasoning",
                                body=thinking,
                                children=[],
                                raw=msg,
                            )
                        )

                    for block in tool_blocks:
                        tool_id = block["id"] if isinstance(block.get("id"), str) else f"{event_id}-{i}-tc"
                        tool_name = block["name"] if isinstance(block.get("name"), str) else "unknown_tool"
                        input_preview = block.get("input_preview")
                        skill_name = (
                            _parse_skill_preview(input_preview)
                            if tool_name == "Skill" and isinstance(input_preview, str)
                            else None
                        )
                        resolved_tool_name = skill_name if (tool_name == "Skill" and skill_name) else tool_name
                        is_skill = tool_name == "Skill" and bool(skill_name)
                        mcp_match = re.match(r"^mcp__([^_]+)__(.+)$", tool_name)
                        is_mcp = bool(mcp_match)
                        trace_tool_name = (
                            f"{mcp_match.group(1)} · {mcp_match.group(2)}" if mcp_match else resolved_tool_name
                        )
                        tool_input = (
                            _format_trace_body(input_preview)
                            if not is_skill and isinstance(input_preview, str)
                            else None
                        )

                        tool_name_map[tool_id] = resolved_tool_name
                        if skill_name:
                            skill_names.add(skill_name)
                        if tool_name.startswith("mcp__"):
                            parts = tool_name.split("__")
                            if len(parts) > 1 and parts[1]:
                                mcp_servers.add(parts[1])
                        stats.tool_calls += 1

                        is_internal_message = tool_name == "SendMessage"
                        message_info = (
                            _parse_send_message_preview(input_preview)
                            if is_internal_message and isinstance(input_preview, str)
                            else (None, None, None)
                        )
                        parent = resolve_msg_parent(msg)
                        sender = (default_agent or "Coordinator") if parent == root else parent.label
                        recipient_id, message_text, _ = message_info
                        message_text = message_text.strip() if message_text else None
                        recipient_id = recipient_id.strip() if recipient_id else None

                        if is_internal_message:
                            message_tool_ids.add(tool_id)

                        if is_internal_message and (not message_text or not recipient_id):
                            continue

                        meta_dict: dict[str, str] = {"tool_use_id": tool_id}
                        if is_internal_message and recipient_id:
                            meta_dict["recipient_id"] = recipient_id
                        if skill_name:
                            meta_dict["skill"] = skill_name

                        tool_node = TraceNodeResponse(
                            id=f"{event_id}-tc-{tool_id}",
                            kind="messaging" if is_internal_message else "tool_call",
                            timestamp=ts,
                            label=f"{sender} -> {recipient_id}" if is_internal_message else trace_tool_name,
                            body=message_text if is_internal_message else (None if is_skill else tool_input),
                            badge="SKILL" if is_skill else ("MCP" if is_mcp else None),
                            children=[],
                            raw=block,
                            meta=meta_dict,
                        )

                        if tool_name == "Agent":
                            agent_name, agent_detail = (
                                _parse_agent_preview(input_preview)
                                if isinstance(input_preview, str)
                                else ("subagent", None)
                            )
                            sub_node = TraceNodeResponse(
                                id=f"{event_id}-sub-{tool_id}",
                                kind="subagent",
                                timestamp=ts,
                                label=agent_name,
                                detail=agent_detail,
                                children=[],
                                raw=block,
                                meta={"tool_use_id": tool_id},
                            )
                            stats.subagent_spawns += 1
                            resolve_msg_parent(msg).children.append(sub_node)
                            active_subagent = sub_node
                            subagent_nodes_by_tool_id[tool_id] = sub_node
                            pending_tool_calls[tool_id] = sub_node
                        else:
                            resolve_msg_parent(msg).children.append(tool_node)
                            pending_tool_calls[tool_id] = tool_node
                    continue

                # Tool result
                if msg_type == "tool_result":
                    tool_use_id = msg.get("tool_use_id") if isinstance(msg.get("tool_use_id"), str) else ""
                    preview = msg.get("content_preview") if isinstance(msg.get("content_preview"), str) else ""
                    is_err = bool(msg.get("is_error"))
                    if is_err:
                        stats.tool_errors += 1
                    result_node = TraceNodeResponse(
                        id=f"{event_id}-tr-{i}",
                        kind="tool_result",
                        timestamp=ts,
                        label="",
                        body=_format_trace_body(preview),
                        is_error=is_err,
                        children=[],
                        raw=msg,
                        meta={"tool_use_id": tool_use_id},
                    )

                    parent_call = pending_tool_calls.get(tool_use_id)
                    if not is_err and tool_use_id in message_tool_ids:
                        pending_tool_calls.pop(tool_use_id, None)
                        continue

                    if parent_call:
                        if parent_call.kind == "messaging":
                            if is_err:
                                parent_call.children.append(result_node)
                            pending_tool_calls.pop(tool_use_id, None)
                            continue
                        if parent_call.kind == "subagent":
                            agent_id_match = re.search(r"agentId:\s*['\"]?([\w-]+)", preview)
                            if agent_id_match:
                                agent_id = agent_id_match.group(1)
                                subagent_names_by_agent_id[agent_id] = parent_call.label
                                parent_call.meta["agent_id"] = agent_id
                            if is_err:
                                parent_call.children.append(result_node)
                            pending_tool_calls.pop(tool_use_id, None)
                            continue
                        parent_call.children.append(result_node)
                        pending_tool_calls.pop(tool_use_id, None)
                    else:
                        resolve_msg_parent(msg).children.append(result_node)
                    continue

                # Unknown tool_result via repr fallback
                if msg_type == "unknown" and isinstance(msg.get("repr"), str):
                    repr_str = msg["repr"]
                    tool_use_id_match = re.search(r"tool_use_id='([^']+)'", repr_str)
                    if tool_use_id_match:
                        tool_use_id = tool_use_id_match.group(1)
                        is_err = bool(re.search(r"is_error=True", repr_str))
                        if is_err:
                            stats.tool_errors += 1
                        content_start = repr_str.find("content=")
                        content_end = repr_str.rfind(", is_error=")
                        body = ""
                        if content_start != -1:
                            if content_end > content_start + 8:
                                body = repr_str[content_start + 8 : content_end]
                            else:
                                body = repr_str[content_start + 8 :]
                            body = re.sub(r"^'", "", body)
                            body = re.sub(r"'\]\)?$", "", body)
                            body = re.sub(r"'$", "", body)
                            body = body.replace(r"\n", "\n").replace(r"\'", "'")

                        result_node = TraceNodeResponse(
                            id=f"{event_id}-utr-{i}",
                            kind="tool_result",
                            timestamp=ts,
                            label="",
                            body=_format_trace_body(body),
                            is_error=is_err,
                            children=[],
                            raw=msg,
                            meta={"tool_use_id": tool_use_id},
                        )

                        parent_call = pending_tool_calls.get(tool_use_id)
                        if not is_err and tool_use_id in message_tool_ids:
                            pending_tool_calls.pop(tool_use_id, None)
                            continue
                        if parent_call:
                            if parent_call.kind == "messaging":
                                if is_err:
                                    parent_call.children.append(result_node)
                                pending_tool_calls.pop(tool_use_id, None)
                                continue
                            if parent_call.kind == "subagent":
                                if is_err:
                                    parent_call.children.append(result_node)
                                pending_tool_calls.pop(tool_use_id, None)
                                continue
                            parent_call.children.append(result_node)
                            pending_tool_calls.pop(tool_use_id, None)
                        else:
                            resolve_msg_parent(msg).children.append(result_node)
                        continue

                # System / subagent progress
                if msg_type == "system":
                    subtype = msg.get("subtype") if isinstance(msg.get("subtype"), str) else "system"
                    sys_data = msg.get("data") if isinstance(msg.get("data"), dict) else {}

                    if subtype == "task_notification":
                        task_id_val = sys_data.get("task_id") if isinstance(sys_data.get("task_id"), str) else None
                        summary_val = sys_data.get("summary") if isinstance(sys_data.get("summary"), str) else None
                        status_val = sys_data.get("status") if isinstance(sys_data.get("status"), str) else None
                        tool_use_id_val = (
                            sys_data.get("tool_use_id") if isinstance(sys_data.get("tool_use_id"), str) else None
                        )
                        subagent = (subagents_by_task_id.get(task_id_val) if task_id_val else None) or (
                            subagent_nodes_by_tool_id.get(tool_use_id_val) if tool_use_id_val else None
                        )
                        if subagent and status_val == "completed" and summary_val and summary_val.strip():
                            if task_id_val:
                                subagents_by_task_id[task_id_val] = subagent
                            existing = next(
                                (c for c in subagent.children if c.meta.get("result_role") == "subagent_return"),
                                None,
                            )
                            if existing:
                                existing.timestamp = ts
                                existing.body = summary_val
                                existing.raw = msg
                            else:
                                subagent.children.append(
                                    TraceNodeResponse(
                                        id=f"{event_id}-sub-result-{i}",
                                        kind="result",
                                        timestamp=ts,
                                        label="Branch findings",
                                        body=summary_val,
                                        children=[],
                                        raw=msg,
                                        meta={"result_role": "subagent_return"},
                                    )
                                )
                        continue

                    if subtype in {
                        "thinking_tokens",
                        "init",
                        "status",
                        "hook_started",
                        "hook_response",
                        "compact_boundary",
                        "task_updated",
                    }:
                        continue

                    if subtype == "task_started":
                        description = (
                            sys_data.get("description")
                            if isinstance(sys_data.get("description"), str)
                            else "Subagent task started"
                        )
                        task_id_val = sys_data.get("task_id") if isinstance(sys_data.get("task_id"), str) else None
                        tool_use_id_val = (
                            sys_data.get("tool_use_id") if isinstance(sys_data.get("tool_use_id"), str) else None
                        )
                        task_type = _summarize_task_type(sys_data.get("task_type"))
                        subagent_node = (
                            subagent_nodes_by_tool_id.get(tool_use_id_val) if tool_use_id_val else None
                        ) or (pending_tool_calls.get(tool_use_id_val) if tool_use_id_val else None)

                        if subagent_node and subagent_node.kind == "subagent":
                            if subagent_node.label == "subagent":
                                subagent_node.label = description
                            subagent_node.detail = task_type or subagent_node.detail or description
                            if task_id_val:
                                subagent_node.meta["task_id"] = task_id_val
                                subagents_by_task_id[task_id_val] = subagent_node
                            active_subagent = subagent_node
                            continue
                        continue

                    if subtype == "task_progress":
                        continue

                    desc = (
                        sys_data.get("description")
                        if isinstance(sys_data.get("description"), str)
                        else (sys_data.get("usage") if isinstance(sys_data.get("usage"), str) else None)
                    )
                    current_parent().children.append(
                        TraceNodeResponse(
                            id=f"{event_id}-sys-{i}",
                            kind="lifecycle",
                            timestamp=ts,
                            label=(default_agent if subtype == "init" and default_agent else subtype),
                            detail="init" if subtype == "init" else desc,
                            badge="AGENT" if subtype == "init" and default_agent else None,
                            children=[],
                            raw=msg,
                        )
                    )
                    continue

                # Coordinator result snapshot
                if msg_type == "result":
                    preview = msg.get("result_preview") if isinstance(msg.get("result_preview"), str) else ""
                    turns = msg.get("num_turns") if isinstance(msg.get("num_turns"), int) else 0
                    stats.total_turns = turns
                    previous_node = root.children[-1] if root.children else None
                    previous_raw = previous_node.raw if previous_node and isinstance(previous_node.raw, dict) else {}
                    if (
                        previous_node
                        and previous_node.kind == "assistant"
                        and not previous_raw.get("parent_tool_use_id")
                        and _is_duplicate_narrative(previous_node.body, preview)
                    ):
                        root.children.pop()

                    res_meta = {"result_role": "session_result"}
                    if turns:
                        res_meta["turns"] = str(turns)

                    res_node = TraceNodeResponse(
                        id=f"{event_id}-res-{i}",
                        kind="assistant",
                        timestamp=ts,
                        label="",
                        body=preview,
                        children=[],
                        raw=msg,
                        meta=res_meta,
                    )
                    result_nodes.append(res_node)
                    root.children.append(res_node)
            continue

        # Top-level events
        if event_type == "session_complete":
            full_result = (
                data.get("result")
                if isinstance(data.get("result"), str)
                else (data.get("result_preview") if isinstance(data.get("result_preview"), str) else None)
            )
            if full_result:
                target_result_node = (
                    result_nodes[-1]
                    if result_nodes
                    else next((n for n in reversed(root.children) if n.kind == "result"), None)
                )
                if target_result_node:
                    target_result_node.kind = "result"
                    target_result_node.label = "Final"
                    if len(full_result) > len(target_result_node.body or ""):
                        target_result_node.body = full_result
                else:
                    root.children.append(
                        TraceNodeResponse(
                            id=f"{event_id}-res",
                            kind="result",
                            timestamp=event_ts,
                            label="Final",
                            body=full_result,
                            children=[],
                            raw=data,
                            meta={"result_role": "session_result"},
                        )
                    )

            if isinstance(data.get("input_tokens"), int):
                stats.tokens_in = data["input_tokens"]
            if isinstance(data.get("output_tokens"), int):
                stats.tokens_out = data["output_tokens"]
            continue

        if event_type in {"session_error", "session_timeout"}:
            root.children.append(
                TraceNodeResponse(
                    id=event_id,
                    kind="error",
                    timestamp=event_ts,
                    label="Session Error" if event_type == "session_error" else "Session Timeout",
                    body=data.get("error") if isinstance(data.get("error"), str) else None,
                    is_error=True,
                    children=[],
                    raw=data,
                )
            )
            continue

        if event_type in {"approval_requested", "permission_callback", "user_question_requested"}:
            current_parent().children.append(
                TraceNodeResponse(
                    id=event_id,
                    kind="hook",
                    timestamp=event_ts,
                    label=event_type.replace("_", " "),
                    detail=(
                        data.get("tool_name")
                        if isinstance(data.get("tool_name"), str)
                        else (data.get("prompt_preview") if isinstance(data.get("prompt_preview"), str) else None)
                    ),
                    children=[],
                    raw=data,
                )
            )
            continue

        if event_type == "hook_event":
            hook_name = data.get("hook_name") if isinstance(data.get("hook_name"), str) else "hook"
            hook_status = data.get("status") if isinstance(data.get("status"), str) else "unknown"
            hook_event = data.get("hook_event") if isinstance(data.get("hook_event"), str) else None
            hook_detail = data.get("detail") if isinstance(data.get("detail"), str) else None

            current_parent().children.append(
                TraceNodeResponse(
                    id=event_id,
                    kind="hook",
                    timestamp=event_ts,
                    label=f"{hook_name} · {hook_status}",
                    detail=hook_event,
                    body=hook_detail,
                    children=[],
                    raw=data,
                )
            )

    # Reorder terminal subagent return and remove duplicate narratives
    for subagent in subagent_nodes_by_tool_id.values():
        branch_result = next((c for c in subagent.children if c.meta.get("result_role") == "subagent_return"), None)
        if not branch_result:
            continue
        subagent.children = [
            c
            for c in subagent.children
            if c is branch_result or c.kind != "assistant" or not _is_duplicate_narrative(c.body, branch_result.body)
        ]
        subagent.children = [c for c in subagent.children if c is not branch_result]
        subagent.children.append(branch_result)

    def replace_recipient_ids(nodes: list[TraceNodeResponse]) -> None:
        for node in nodes:
            if node.kind == "messaging" and node.meta.get("recipient_id"):
                recipient = subagent_names_by_agent_id.get(node.meta["recipient_id"])
                if recipient:
                    node.label = node.label.replace(node.meta["recipient_id"], recipient)
                    node.meta["recipient"] = recipient
            replace_recipient_ids(node.children)

    replace_recipient_ids(root.children)

    return SessionTraceResponse(
        root=root,
        stats=stats,
        heartbeats=heartbeats,
        skills_used=sorted(skill_names),
        mcps_used=sorted(mcp_servers),
        event_count=len(events),
    )
