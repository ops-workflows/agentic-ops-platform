"""Fake Hindsight service.

Implements only the subset the platform uses:

- GET  /health                       — healthcheck
- GET  /v1/banks                     — list configured banks
- POST /v1/banks/{bank}/retain       — store an item
- POST /v1/banks/{bank}/recall       — recall similar items
- POST /v1/banks/{bank}/reflect      — reflection stub

All requests are recorded for assertions. Responses are fully scriptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from fastapi import FastAPI


@dataclass
class FakeHindsightState:
    banks: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": "platform-test-bank", "description": "test bank"},
        ]
    )
    retained: list[dict[str, Any]] = field(default_factory=list)
    recall_queue: list[dict[str, Any]] = field(default_factory=list)
    reflect_queue: list[dict[str, Any]] = field(default_factory=list)
    recorded: list[dict[str, Any]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


class FakeHindsight:
    def __init__(self) -> None:
        self.state = FakeHindsightState()
        self.app = self._build_app()

    # ── Test helpers ─────────────────────────────────────────

    def script_recall(self, items: list[dict[str, Any]]) -> None:
        with self.state.lock:
            self.state.recall_queue.append({"items": items})

    def script_reflect(self, response: dict[str, Any]) -> None:
        with self.state.lock:
            self.state.reflect_queue.append(response)

    def recorded_requests(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return list(self.state.recorded)

    def retained_items(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return list(self.state.retained)

    def reset(self) -> None:
        with self.state.lock:
            self.state = FakeHindsightState()

    # ── FastAPI app ──────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Fake Hindsight")

        def _retain_impl(bank: str, body: dict):
            with self.state.lock:
                self.state.recorded.append({"op": "retain", "bank": bank, "body": body})
                self.state.retained.append({"bank": bank, "body": body})
            return {"status": "ok", "id": f"item-{len(self.state.retained)}"}

        def _recall_impl(bank: str, body: dict):
            with self.state.lock:
                self.state.recorded.append({"op": "recall", "bank": bank, "body": body})
                if self.state.recall_queue:
                    return self.state.recall_queue.pop(0)
            return {"items": []}

        def _reflect_impl(bank: str, body: dict):
            with self.state.lock:
                self.state.recorded.append({"op": "reflect", "bank": bank, "body": body})
                if self.state.reflect_queue:
                    return self.state.reflect_queue.pop(0)
            return {"patterns": []}

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get("/v1/banks")
        def list_banks():
            with self.state.lock:
                self.state.recorded.append({"op": "list_banks"})
                return {"banks": list(self.state.banks)}

        @app.post("/v1/banks/{bank}/retain")
        def retain(bank: str, body: dict):
            return _retain_impl(bank, body)

        @app.post("/v1/banks/{bank}/recall")
        def recall(bank: str, body: dict):
            return _recall_impl(bank, body)

        @app.post("/v1/banks/{bank}/reflect")
        def reflect(bank: str, body: dict):
            return _reflect_impl(bank, body)

        @app.post("/v1/default/banks/{bank}/memories")
        def retain_memory(bank: str, body: dict):
            return _retain_impl(bank, body)

        @app.post("/v1/default/banks/{bank}/memories/recall")
        def recall_memory(bank: str, body: dict):
            return _recall_impl(bank, body)

        @app.post("/v1/default/banks/{bank}/reflect")
        def reflect_default(bank: str, body: dict):
            return _reflect_impl(bank, body)

        return app


def build_fake_hindsight() -> FakeHindsight:
    return FakeHindsight()
