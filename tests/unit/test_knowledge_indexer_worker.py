"""Dedicated Knowledge Source worker scheduling tests."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _worker_module():
    path = Path(__file__).parents[2] / "knowledge-indexer" / "main.py"
    spec = importlib.util.spec_from_file_location("knowledge_indexer_main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_schedule_requires_explicit_positive_interval() -> None:
    worker = _worker_module()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    assert worker.source_is_due({}, last_started_at=None, now=now) is False
    assert worker.source_is_due({"interval_sec": True}, last_started_at=None, now=now) is False
    assert worker.source_is_due({"interval_sec": 0}, last_started_at=None, now=now) is False
    assert worker.source_is_due({"interval_sec": 300}, last_started_at=None, now=now) is True
    assert (
        worker.source_is_due(
            {"interval_sec": 300},
            last_started_at=now - timedelta(seconds=299),
            now=now,
        )
        is False
    )
    assert (
        worker.source_is_due(
            {"interval_sec": 300},
            last_started_at=now - timedelta(seconds=300),
            now=now,
        )
        is True
    )
