"""Unit tests for DST/timezone correctness of the cron scheduler.

The scheduler uses APScheduler's ``CronTrigger`` with UTC. These tests verify
that:
- cron expressions are evaluated consistently regardless of host local time
- ``compute_next_run`` always returns a future timestamp
- DST jumps do not produce duplicate or missed fire times when interpreted
  in UTC (the platform's fixed policy)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gateway.scheduler import _parse_cron, compute_next_run

pytestmark = pytest.mark.unit


def test_compute_next_run_returns_future_isoformat():
    result = compute_next_run("*/15 * * * *")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    now = datetime.now(UTC)
    assert parsed >= now


def test_parse_cron_rejects_wrong_field_count():
    with pytest.raises(ValueError):
        _parse_cron("0 9 * *")  # 4 fields
    with pytest.raises(ValueError):
        _parse_cron("0 9 * * * *")  # 6 fields


def test_cron_trigger_uses_utc_not_local_time():
    """CronTrigger.get_next_fire_time called with UTC now returns UTC."""
    trigger = _parse_cron("0 9 * * *")
    anchor = datetime(2026, 3, 8, 0, 0, tzinfo=UTC)  # DST spring-forward day in US
    next_fire = trigger.get_next_fire_time(None, anchor)
    assert next_fire is not None
    # The trigger should fire at 09:00 UTC regardless of DST
    assert next_fire.hour == 9
    assert next_fire.minute == 0


def test_cron_trigger_spans_dst_boundary_without_duplicates():
    """Evaluate an hourly trigger across the DST weekend; no duplicate UTC times."""
    trigger = _parse_cron("0 * * * *")
    anchor = datetime(2026, 3, 7, 0, 0, tzinfo=UTC)
    fires: list[datetime] = []
    cur = anchor
    for _ in range(72):  # 3 days of hourly fires
        nxt = trigger.get_next_fire_time(None, cur)
        if nxt is None:
            break
        fires.append(nxt)
        cur = nxt + timedelta(seconds=1)
    # Every fire time must be strictly increasing — no DST-induced repeats
    assert all(fires[i] < fires[i + 1] for i in range(len(fires) - 1))
    # 72 hourly fires over 3 days should cover exactly 72 distinct UTC hours
    assert len(set(fires)) == len(fires)


def test_compute_next_run_returns_none_on_invalid():
    assert compute_next_run("not a cron") is None
    assert compute_next_run("") is None
