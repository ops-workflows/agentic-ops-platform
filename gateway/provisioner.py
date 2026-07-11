"""Provisioner — discovers and registers workflows from configured roots.

On startup and on GitHub webhook, scans configured workflow roots for
agent.yaml files.
Registers agents in the control_plane.agents table, creates Docker volumes
for memory, registers schedules, and validates workflow directory structure.

Workflows use the flat layout convention (agents/, skills/, hooks/hooks.json,
settings.json at root). The provisioner validates they exist but does not
generate them.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from gateway.plugin_dir import validate_plugin_dir
from shared.lib.db import async_session_factory
from shared.lib.models import Agent, Schedule
from shared.lib.workflow_paths import discover_workflow_packages, workflow_repo_path

logger = logging.getLogger(__name__)


def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _scan_workflows() -> list[tuple[str, dict, str]]:
    """Scan configured workflow roots for agent.yaml files.

    Returns list of (repo_path, parsed_config, raw_yaml) tuples.
    """
    results = []

    for package in discover_workflow_packages():
        try:
            raw = package.raw_yaml
            config = package.config
            results.append((workflow_repo_path(package), config, raw))

            # Validate Claude Code directory structure
            errors = validate_plugin_dir(package.path)
            if errors:
                logger.warning(
                    "Workflow '%s' has validation issues: %s",
                    config.get("name", package.path.name),
                    "; ".join(errors),
                )

            logger.info("Found workflow: %s", config.get("name", package.path.name))
        except Exception:
            logger.exception("Failed to parse %s", package.agent_yaml_path)

    return results


async def run_provisioner_scan() -> None:
    """Scan workflow roots and register/update agents in Postgres."""
    workflows = _scan_workflows()
    logger.info("Provisioner scan found %d workflow(s)", len(workflows))
    discovered_names = {
        str(config.get("name", "unknown"))
        for _, config, _ in workflows
        if isinstance(config, dict) and config.get("name")
    }

    async with async_session_factory() as session:
        existing_agents = (await session.execute(select(Agent))).scalars().all()

        for repo_path, config, raw_yaml in workflows:
            name = config.get("name", "unknown")
            config_hash = _compute_hash(raw_yaml)

            # Check if agent already exists
            result = await session.execute(select(Agent).where(Agent.name == name))
            existing = result.scalar_one_or_none()

            if existing:
                if existing.config_hash == config_hash:
                    if not existing.provisioned:
                        existing.provisioned = True
                        existing.provisioned_at = existing.provisioned_at or datetime.now(UTC)
                        await session.commit()
                    logger.info("Agent '%s' unchanged, skipping", name)
                    continue

                # Update existing agent
                existing.description = config.get("description", "")
                existing.version = config.get("version", "")
                existing.config = config
                existing.config_hash = config_hash
                existing.repo_path = repo_path
                existing.provisioned = True
                existing.provisioned_at = existing.provisioned_at or datetime.now(UTC)
                logger.info("Agent '%s' config changed, updating", name)
            else:
                # Register new agent
                agent = Agent(
                    name=name,
                    description=config.get("description", ""),
                    version=config.get("version", ""),
                    config=config,
                    config_hash=config_hash,
                    repo_path=repo_path,
                    provisioned=True,
                    provisioned_at=datetime.now(UTC),
                )
                session.add(agent)
                logger.info("Registered new agent '%s'", name)

            # Sync schedules
            await _sync_schedules(session, name, config.get("schedules", []))

        for existing in existing_agents:
            if existing.name in discovered_names:
                continue
            if existing.provisioned:
                existing.provisioned = False
                logger.info("Marked stale agent '%s' as unprovisioned", existing.name)

            existing_schedules = await session.execute(select(Schedule).where(Schedule.agent_id == existing.id))
            for sched in existing_schedules.scalars().all():
                await session.delete(sched)

        await session.commit()


async def _sync_schedules(session, agent_name: str, schedules_config: list[dict]) -> None:
    """Sync schedule definitions from agent.yaml to Postgres."""
    result = await session.execute(select(Agent).where(Agent.name == agent_name))
    agent = result.scalar_one_or_none()
    if not agent:
        return

    existing_result = await session.execute(select(Schedule).where(Schedule.agent_id == agent.id))
    existing_by_name = {sched.name: sched for sched in existing_result.scalars().all()}

    configured_names: set[str] = set()

    for sched_config in schedules_config:
        schedule_name = str(sched_config.get("name", ""))
        if not schedule_name:
            continue

        configured_names.add(schedule_name)
        existing = existing_by_name.get(schedule_name)
        if existing:
            existing.cron = str(sched_config.get("cron", ""))
            existing.prompt = str(sched_config.get("prompt", ""))
            existing.enabled = bool(sched_config.get("enabled", True))
            logger.info("Updated schedule '%s' for agent '%s'", schedule_name, agent_name)
            continue

        schedule = Schedule(
            agent_id=agent.id,
            name=schedule_name,
            cron=str(sched_config.get("cron", "")),
            prompt=str(sched_config.get("prompt", "")),
            enabled=bool(sched_config.get("enabled", True)),
        )
        session.add(schedule)
        logger.info("Registered schedule '%s' for agent '%s'", schedule_name, agent_name)

    for schedule_name, existing in existing_by_name.items():
        if schedule_name not in configured_names:
            await session.delete(existing)
