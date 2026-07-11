"""Scripted Anthropic-compatible LLM mock server.

Fidelity goal: the minimum needed by the Claude CLI in the runtime container
to execute scripted tool-using conversations, with recording for assertions.

Two response modes:

1. **Scripted mode** — a list of `Turn` objects is consumed in order, each
   producing a fixed assistant response. Use this for behavioral
   scenarios (tool_use, multi-turn, end_turn).

2. **Probe/echo mode** — a per-turn ``probe`` callable inspects the inbound
   request and returns a compact summary string (e.g. "found markers: [...]
   missing markers: [...]"). The summary is wrapped as a single text
   block. Use this for instruction-surface tests where the test only
   needs to know what the runtime sent upstream.

A scenario consists of a list of `Turn` objects:

    Turn(
        expect={
            "tools_present": [...],
            "tools_absent": [...],
            "markers_present": [...],
            "tool_result_for": "Bash",
        },
        respond=[
            {"type": "text", "text": "..."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
        ],
        stop_reason="tool_use",  # or "end_turn"
        usage={"input_tokens": 10, "output_tokens": 8},
        probe=lambda body: "found_markers=[CLAUDE_MD_OK]",  # optional
    )

The server consumes turns in order. Each inbound `/v1/messages` request is
matched against the next pending turn's `expect` clause (soft assertions —
tests later read `server.recorded_requests()` for strict assertions).

Supports streaming SSE (which the Claude CLI expects) and non-streaming.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass
class Turn:
    respond: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 1, "output_tokens": 1})
    # Optional callable: receives the parsed request body and returns
    # a string summary. When set, the assistant response is replaced
    # with a single text block containing that summary.
    probe: Callable[[dict[str, Any]], str] | None = None


# ── Probe helpers ─────────────────────────────────────────────────


def request_text_blob(body: dict[str, Any]) -> str:
    """Concatenate everything the runtime sent the LLM into one string.

    Used by probe callables to scan for markers across the system prompt,
    user turn text, tool definitions, and tool_result payloads.
    """
    system = body.get("system") or ""
    if isinstance(system, list):
        system = "\n".join(str(s.get("text", "")) for s in system if isinstance(s, dict))
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    return str(system) + "\n" + json.dumps(messages, ensure_ascii=False) + "\n" + json.dumps(tools, ensure_ascii=False)


def scan_markers(body: dict[str, Any], markers: Iterable[str]) -> dict[str, list[str]]:
    """Return ``{"found": [...], "missing": [...]}`` for ``markers`` in the request.

    Marker strings are looked up as plain substrings of the concatenated
    request blob (system + messages + tools).
    """
    blob = request_text_blob(body)
    found: list[str] = []
    missing: list[str] = []
    for m in markers:
        if m in blob:
            found.append(m)
        else:
            missing.append(m)
    return {"found": found, "missing": missing}


def make_marker_probe(markers: Iterable[str]) -> Callable[[dict[str, Any]], str]:
    """Build a probe callable that summarises marker presence.

    Returns a string of the shape:
      ``found_markers=[A,B] missing_markers=[C]``
    """
    marker_list = list(markers)

    def _probe(body: dict[str, Any]) -> str:
        result = scan_markers(body, marker_list)
        return "found_markers=[" + ",".join(result["found"]) + "] missing_markers=[" + ",".join(result["missing"]) + "]"

    return _probe


def list_tools_probe(body: dict[str, Any]) -> str:
    """Probe that lists the tool names exposed to the model in this request."""
    tools = body.get("tools") or []
    names = [str(t.get("name", "")) for t in tools if isinstance(t, dict)]
    return "tools_exposed=[" + ",".join(names) + "]"


@dataclass
class _State:
    turns: list[Turn] = field(default_factory=list)
    cursor: int = 0
    recorded: list[dict[str, Any]] = field(default_factory=list)
    expectation_failures: list[str] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


class MockLLMServer:
    """Anthropic-compatible scripted server.

    Not a full Anthropic clone. Implements the response-shape surface the
    pinned Claude CLI uses in scripted scenarios.
    """

    def __init__(self, turns: list[Turn] | None = None) -> None:
        self.state = _State(turns=list(turns or []))
        self.app = self._build_app()

    # ── Test helpers ──────────────────────────────────────────

    def set_scenario(self, turns: Iterable[Turn]) -> None:
        with self.state.lock:
            self.state.turns = list(turns)
            self.state.cursor = 0
            self.state.recorded.clear()
            self.state.expectation_failures.clear()

    def recorded_requests(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return list(self.state.recorded)

    def expectation_failures(self) -> list[str]:
        with self.state.lock:
            return list(self.state.expectation_failures)

    def reset(self) -> None:
        self.set_scenario([])

    # ── Internals ─────────────────────────────────────────────

    def _next_turn(self) -> Turn:
        with self.state.lock:
            if self.state.cursor >= len(self.state.turns):
                turn = Turn(
                    respond=[{"type": "text", "text": "[mock-llm: out of scripted turns]"}],
                    stop_reason="end_turn",
                )
                self.state.expectation_failures.append(
                    f"requested turn #{self.state.cursor + 1} but only {len(self.state.turns)} scripted"
                )
            else:
                turn = self.state.turns[self.state.cursor]
            self.state.cursor += 1
            return turn

    def _check_expectations(self, request_body: dict[str, Any], expect: dict[str, Any]) -> None:
        if not expect:
            return

        tools = request_body.get("tools") or []
        tool_names = {t.get("name") for t in tools if isinstance(t, dict)}

        messages = request_body.get("messages") or []
        system = request_body.get("system") or ""
        if isinstance(system, list):
            system = "\n".join(str(s.get("text", "")) for s in system if isinstance(s, dict))
        all_text_blob = str(system) + "\n" + json.dumps(messages, ensure_ascii=False)

        failures: list[str] = []
        for name in expect.get("tools_present", []) or []:
            if name not in tool_names:
                failures.append(f"expected tool present: {name}")
        for name in expect.get("tools_absent", []) or []:
            if name in tool_names:
                failures.append(f"expected tool absent: {name}")
        for marker in expect.get("markers_present", []) or []:
            if marker not in all_text_blob:
                failures.append(f"expected marker present: {marker}")
        for marker in expect.get("markers_absent", []) or []:
            if marker in all_text_blob:
                failures.append(f"expected marker absent: {marker}")

        expected_tool_result = expect.get("tool_result_for")
        if expected_tool_result:
            found = False
            for m in messages:
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and str(block.get("tool_use_id", "")).startswith("")
                    ):
                        found = True
                        break
            if not found:
                failures.append(f"expected tool_result for tool: {expected_tool_result}")

        if failures:
            with self.state.lock:
                self.state.expectation_failures.extend(failures)

    def _build_response_body(self, turn: Turn, model: str, request_body: dict[str, Any]) -> dict[str, Any]:
        # Probe mode: summarise the inbound request as the assistant text.
        if turn.probe is not None:
            try:
                summary = turn.probe(request_body)
            except Exception as exc:  # pragma: no cover - defensive
                summary = f"[probe-error: {exc!r}]"
            content_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": str(summary)},
            ]
        else:
            content_blocks = []
            for block in turn.respond:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    content_blocks.append({"type": "text", "text": str(block.get("text", ""))})
                elif block.get("type") == "tool_use":
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                            "name": block.get("name"),
                            "input": block.get("input") or {},
                        }
                    )
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content_blocks or [{"type": "text", "text": ""}],
            "stop_reason": turn.stop_reason,
            "stop_sequence": None,
            "usage": turn.usage,
        }

    def _sse_stream(self, body: dict[str, Any]) -> Iterable[bytes]:
        def evt(event: str, data: dict[str, Any]) -> bytes:
            return (f"event: {event}\n" + f"data: {json.dumps(data)}\n\n").encode("utf-8")

        msg_start = {
            "type": "message_start",
            "message": {
                "id": body["id"],
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": body["usage"],
            },
        }
        yield evt("message_start", msg_start)

        for idx, block in enumerate(body["content"]):
            if block["type"] == "text":
                yield evt(
                    "content_block_start",
                    {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
                )
                yield evt(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": block["text"]},
                    },
                )
                yield evt("content_block_stop", {"type": "content_block_stop", "index": idx})
            elif block["type"] == "tool_use":
                yield evt(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {},
                        },
                    },
                )
                yield evt(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block.get("input") or {}),
                        },
                    },
                )
                yield evt("content_block_stop", {"type": "content_block_stop", "index": idx})

        yield evt(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
                "usage": body["usage"],
            },
        )
        yield evt("message_stop", {"type": "message_stop"})

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Mock LLM (Anthropic-compatible)")

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.post("/v1/messages")
        async def messages(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}

            with self.state.lock:
                self.state.recorded.append(
                    {
                        "ts": time.time(),
                        "headers": dict(request.headers),
                        "body": body,
                    }
                )

            turn = self._next_turn()
            self._check_expectations(body, turn.expect)
            response_body = self._build_response_body(turn, str(body.get("model") or "claude-test"), body)

            if bool(body.get("stream")):
                return StreamingResponse(
                    self._sse_stream(response_body),
                    media_type="text/event-stream",
                )
            return JSONResponse(response_body)

        return app


def build_mock_llm(turns: list[Turn] | None = None) -> MockLLMServer:
    return MockLLMServer(turns=turns)
