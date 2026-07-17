"""Unit tests for the shared auto_recall hook."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def _load_auto_recall_hook_module():
    path = Path(__file__).resolve().parents[2] / "hooks" / "auto_recall_hook.py"
    spec = importlib.util.spec_from_file_location("auto_recall_hook_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_auto_recall_runs_only_once_per_task(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_ID", "task-123")
    monkeypatch.setenv("TASK_WORKFLOW", "platform-test")
    monkeypatch.setenv("HINDSIGHT_URL", "http://fake-hindsight:8888")
    monkeypatch.setenv("GATEWAY_EVENT_URL", "http://fake-gateway:8080/events")

    hook = _load_auto_recall_hook_module()
    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))

    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, json: dict, timeout: float):
        calls.append((url, json))
        if url.endswith("/memories/recall"):
            return _Response({"items": [{"id": "1", "text": "similar incident"}]})
        return _Response({})

    def fake_get(url: str, timeout: float):
        assert url.endswith("/api/platform/memories")
        return _Response(
            {
                "hindsight_banks": [
                    {
                        "bank_id": "incident-rca-test",
                        "kind": "business",
                        "workflows": ["platform-test"],
                    }
                ]
            }
        )

    monkeypatch.setattr(hook.httpx, "post", fake_post)
    monkeypatch.setattr(hook.httpx, "get", fake_get)

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "Investigate alert"})))
    first_stdout = io.StringIO()
    first_stderr = io.StringIO()
    with redirect_stdout(first_stdout), redirect_stderr(first_stderr):
        hook.main()

    first_output = first_stdout.getvalue()
    assert "similar incident" in first_output
    recall_calls = [url for url, body in calls if url.endswith("/memories/recall")]
    assert len(recall_calls) == 1

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "Continue with retained result"})))
    second_stdout = io.StringIO()
    second_stderr = io.StringIO()
    with redirect_stdout(second_stdout), redirect_stderr(second_stderr):
        hook.main()

    assert second_stdout.getvalue() == ""
    recall_calls = [url for url, body in calls if url.endswith("/memories/recall")]
    assert len(recall_calls) == 1
    assert "Recall already injected for this task" in second_stderr.getvalue() or any(
        body.get("data", {}).get("detail") == "Recall already injected for this task"
        for url, body in calls
        if url.endswith("/events")
    )


def test_auto_recall_timeout_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_ID", "task-timeout")
    monkeypatch.setenv("TASK_WORKFLOW", "platform-test")
    hook = _load_auto_recall_hook_module()
    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(hook, "resolve_bank_id", lambda workflow: "test-bank")

    def fake_post(url: str, json: dict, timeout: float):
        if url.endswith("/memories/recall"):
            assert timeout == 20.0
            raise hook.httpx.TimeoutException("timed out")
        return _Response({})

    monkeypatch.setattr(hook.httpx, "post", fake_post)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "Investigate alert"})))
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        hook.main()

    assert stdout.getvalue() == ""
    assert "Hindsight auto-recall timed out" in stderr.getvalue()
