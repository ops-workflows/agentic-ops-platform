"""Layer 0 — scheduler cron helpers."""

from __future__ import annotations

from datetime import datetime

import pytest

pytestmark = pytest.mark.unit

from gateway.scheduler import compute_next_run  # noqa: E402


def test_compute_next_run_accepts_standard_cron() -> None:
    result = compute_next_run("0 9 * * *")
    assert result is not None
    # Valid ISO timestamp
    datetime.fromisoformat(result)


def test_compute_next_run_invalid_cron_returns_none() -> None:
    assert compute_next_run("not a cron") is None


def test_compute_next_run_empty_returns_none() -> None:
    assert compute_next_run("") is None
