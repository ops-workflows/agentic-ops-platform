"""Layer 1 — provisioner scan registers agent rows."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.service

from gateway.provisioner import run_provisioner_scan  # noqa: E402
from shared.lib.models import Agent, Schedule  # noqa: E402


@pytest.mark.asyncio
async def test_provisioner_registers_platform_test_plugin(
    db_session,
    fixture_workflows_dir: Path,
) -> None:
    from shared.lib.config import settings

    with patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)):
        await run_provisioner_scan()

    agents = (await db_session.execute(select(Agent))).scalars().all()
    by_name = {a.name: a for a in agents}
    assert "platform-test" in by_name
    agent = by_name["platform-test"]
    assert agent.provisioned is True
    assert agent.config.get("name") == "platform-test"

    schedules = (await db_session.execute(select(Schedule).where(Schedule.agent_id == agent.id))).scalars().all()
    assert any(s.name == "test-daily" for s in schedules)


@pytest.mark.asyncio
async def test_provisioner_idempotent_on_unchanged_config(
    db_session,
    fixture_workflows_dir: Path,
) -> None:
    from shared.lib.config import settings

    with patch.object(settings, "workflow_repo_paths", str(fixture_workflows_dir)):
        await run_provisioner_scan()
        await run_provisioner_scan()

    agents = (await db_session.execute(select(Agent).where(Agent.name == "platform-test"))).scalars().all()
    assert len(agents) == 1
