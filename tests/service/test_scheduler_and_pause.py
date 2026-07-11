"""Layer 1 — schedule firing and agent pause/resume."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

pytestmark = pytest.mark.service


async def _make_client(fixture_workflows_dir: Path) -> httpx.AsyncClient:
    from shared.lib.config import settings

    settings.workflow_repo_paths = str(fixture_workflows_dir)
    from gateway.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


# ── Agent pause/resume ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_pause_and_resume_toggles_paused_flag(async_engine, fixture_workflows_dir: Path) -> None:
    from gateway.provisioner import run_provisioner_scan
    from shared.lib.config import settings
    from shared.lib.models import Agent

    with patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)):
        await run_provisioner_scan()

    async with await _make_client(fixture_workflows_dir) as client:
        # Pause
        resp = await client.post("/api/agents/platform-test/pause")
        assert resp.status_code == 200, resp.text
        assert resp.json()["paused"] is True

        # Resume
        resp = await client.post("/api/agents/platform-test/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["paused"] is False

    # Verify DB state
    from shared.lib.db import async_session_factory

    async with async_session_factory() as session:
        agent = (await session.execute(select(Agent).where(Agent.name == "platform-test"))).scalar_one()
        assert agent.paused is False


@pytest.mark.asyncio
async def test_pause_unknown_agent_returns_404(async_engine, fixture_workflows_dir: Path) -> None:
    async with await _make_client(fixture_workflows_dir) as client:
        resp = await client.post("/api/agents/does-not-exist/pause")
        assert resp.status_code == 404


# ── Scheduler handler creates tasks ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_handler_creates_task_and_updates_last_run(
    async_engine, fixture_workflows_dir: Path, db_session
) -> None:
    """Calling the schedule handler directly should queue a task and stamp last_run."""
    from gateway.provisioner import run_provisioner_scan
    from gateway.scheduler import _schedule_job_handler
    from shared.lib.config import settings
    from shared.lib.models import Agent, Schedule, Task

    with patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)):
        await run_provisioner_scan()

    # Fire the scheduled handler directly (simulates a cron fire).
    await _schedule_job_handler(
        agent_name="platform-test",
        schedule_name="test-daily",
        prompt="scheduled test prompt",
        message_channel="platform-test-channel",
    )

    # Verify task was created with triggered_by=scheduler
    tasks = (await db_session.execute(select(Task))).scalars().all()
    scheduled = [t for t in tasks if (t.task_metadata or {}).get("triggered_by") == "scheduler"]
    assert len(scheduled) == 1
    t = scheduled[0]
    assert t.workflow == "platform-test"
    assert t.prompt == "scheduled test prompt"
    assert t.message_channel == "platform-test-channel"
    assert (t.task_metadata or {}).get("schedule") == "test-daily"

    # Verify last_run was stamped
    agent = (await db_session.execute(select(Agent).where(Agent.name == "platform-test"))).scalar_one()
    sched = (
        await db_session.execute(select(Schedule).where(Schedule.agent_id == agent.id, Schedule.name == "test-daily"))
    ).scalar_one()
    assert sched.last_run is not None


@pytest.mark.asyncio
async def test_scheduler_executes_registered_job_and_queues_task(
    async_engine, fixture_workflows_dir: Path, db_session
) -> None:
    from gateway.provisioner import run_provisioner_scan
    from gateway.scheduler import start_scheduler, stop_scheduler
    from shared.lib.config import settings
    from shared.lib.models import Agent, Schedule, Task

    run_at = datetime.now(UTC) + timedelta(seconds=1)

    with (
        patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)),
        patch("gateway.scheduler._parse_cron", return_value=DateTrigger(run_date=run_at)),
    ):
        await run_provisioner_scan()

        agent = (await db_session.execute(select(Agent).where(Agent.name == "platform-test"))).scalar_one()
        agent_id = agent.id

        await start_scheduler()

        try:
            deadline = asyncio.get_running_loop().time() + 5
            scheduled_task = None
            sched = None
            while asyncio.get_running_loop().time() < deadline:
                db_session.expire_all()
                tasks = (await db_session.execute(select(Task).where(Task.workflow == "platform-test"))).scalars().all()
                scheduled_task = next(
                    (task for task in tasks if (task.task_metadata or {}).get("triggered_by") == "scheduler"),
                    None,
                )
                sched = (
                    await db_session.execute(
                        select(Schedule).where(Schedule.agent_id == agent_id, Schedule.name == "test-daily")
                    )
                ).scalar_one()
                if scheduled_task is not None and sched.last_run is not None:
                    break
                await asyncio.sleep(0.1)

            assert scheduled_task is not None
            assert scheduled_task.prompt == "run the synthetic daily test workflow"
            assert scheduled_task.message_channel == "platform-test-channel"

            assert sched is not None
            assert sched.last_run is not None
        finally:
            await stop_scheduler()
