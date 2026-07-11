from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from shared.lib.housekeeping import prune_background_job_runs
from shared.lib.models import BackgroundJobRun

pytestmark = pytest.mark.asyncio


async def test_prune_background_job_runs_keeps_latest_rows(db_session):
    anchor = datetime(2025, 1, 10, tzinfo=UTC)
    for index in range(7):
        db_session.add(
            BackgroundJobRun(
                job_type="housekeeping",
                scope="platform",
                status="succeeded",
                started_at=anchor + timedelta(minutes=index),
                finished_at=anchor + timedelta(minutes=index, seconds=1),
                duration_sec=1.0,
                summary={"run": index},
                warnings=[],
            )
        )
    await db_session.commit()

    pruned = await prune_background_job_runs(db_session, keep_latest=4)

    assert pruned == 3

    rows = (
        (
            await db_session.execute(
                select(BackgroundJobRun).order_by(BackgroundJobRun.started_at.desc(), BackgroundJobRun.id.desc())
            )
        )
        .scalars()
        .all()
    )
    assert [row.summary["run"] for row in rows] == [6, 5, 4, 3]
