"""Session Manager — queue consumer loop.

Background worker (no HTTP). Polls the Postgres task queue, spawns Docker
containers for agent sessions, monitors their lifecycle, and syncs memory to MinIO.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from session_manager.container_lifecycle import monitor_containers
from session_manager.heartbeat import heartbeat_monitor
from session_manager.housekeeping import housekeeping_loop
from session_manager.memory_sync import restore_memory
from sqlalchemy import select

from gateway.plugin_dir import discover_all_plugin_configs
from shared.lib.config import settings
from shared.lib.db import async_session_factory, ensure_runtime_schema
from shared.lib.models import Agent
from shared.lib.task_queue import dequeue_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False
_plugin_worker_cache: dict[str, tuple[int, int]] = {}
_plugin_worker_cache_loaded_at = 0.0
_PLUGIN_SCAN_INTERVAL_SEC = 30.0

_WORKFLOW_PRIORITY = {"high": 0, "medium": 1, "low": 2}


def _workflow_priority(config: dict) -> int:
    value = config.get("runtime", {}).get("priority", "medium")
    if isinstance(value, int) and value in (1, 2, 3):
        return value - 1
    return _WORKFLOW_PRIORITY.get(str(value).strip().lower(), _WORKFLOW_PRIORITY["medium"])


def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down gracefully", sig)
    _shutdown = True


def _refresh_plugin_worker_cache(force: bool = False) -> dict[str, tuple[int, int]]:
    """Refresh workflow concurrency caps and queue priorities at a bounded cadence."""
    global _plugin_worker_cache, _plugin_worker_cache_loaded_at

    now = time.monotonic()
    if not force and _plugin_worker_cache and now - _plugin_worker_cache_loaded_at < _PLUGIN_SCAN_INTERVAL_SEC:
        return _plugin_worker_cache

    plugins: dict[str, tuple[int, int]] = {}
    for workflow, config in discover_all_plugin_configs():
        max_workers = config.get("runtime", {}).get("parallel_workers", 1)
        plugins[workflow] = (max(1, int(max_workers)), _workflow_priority(config))

    _plugin_worker_cache = plugins
    _plugin_worker_cache_loaded_at = now
    logger.info("Refreshed plugin worker limits for %d workflow(s)", len(plugins))
    return plugins


async def queue_consumer_loop() -> None:
    """Main loop: poll queue → restore memory → spawn → monitor → cleanup."""
    logger.info("Session Manager starting — poll interval: %ds", settings.poll_interval_sec)

    while not _shutdown:
        try:
            plugins = _refresh_plugin_worker_cache()

            async with async_session_factory() as session:
                result = await session.execute(select(Agent.name, Agent.provisioned, Agent.paused))
                agent_state = {
                    str(name): {"provisioned": bool(provisioned), "paused": bool(paused)}
                    for name, provisioned, paused in result.all()
                }

                enabled_plugins = {
                    workflow: settings_for_workflow
                    for workflow, settings_for_workflow in plugins.items()
                    if (workflow_state := agent_state.get(workflow))
                    and workflow_state["provisioned"]
                    and not workflow_state["paused"]
                }
                task = await dequeue_task(
                    session,
                    platform_max_running=settings.platform_max_running_tasks,
                    workflow_limits={workflow: values[0] for workflow, values in enabled_plugins.items()},
                    workflow_priorities={workflow: values[1] for workflow, values in enabled_plugins.items()},
                )

            if task:
                logger.info("Dequeued task %s (workflow=%s)", task.id, task.workflow)

                task_id = str(task.id)
                if task.container_id:
                    from session_manager.container_lifecycle import has_live_runtime

                    from shared.lib.task_queue import complete_task

                    if has_live_runtime(task_id, container_id=task.container_id):
                        logger.info("Admitted existing runtime %s for resumed task %s", task.container_id, task.id)
                        continue

                    logger.error(
                        "Resumed task %s references runtime %s, but no live runtime was found; "
                        "JSONL restart is not implemented",
                        task.id,
                        task.container_id,
                    )
                    async with async_session_factory() as session:
                        await complete_task(
                            session,
                            task.id,
                            status="failed",
                            error=(
                                "Waiting task lost its live runtime before scheduler admission; "
                                "resume restart is not implemented"
                            ),
                        )
                    continue

                # Restore agent memory from MinIO if volume is empty
                await restore_memory(task.workflow)

                # Spawn agent container
                from session_manager.container_lifecycle import spawn_agent_session

                container = await spawn_agent_session(task)
                if container:
                    logger.info("Spawned container %s for task %s", container.short_id, task.id)
                else:
                    logger.error("Failed to spawn container for task %s", task.id)
            else:
                await asyncio.sleep(settings.poll_interval_sec)

        except Exception:
            logger.exception("Error in queue consumer loop")
            await asyncio.sleep(settings.poll_interval_sec)


async def run() -> None:
    """Run all Session Manager coroutines concurrently."""
    await ensure_runtime_schema()
    await asyncio.gather(
        queue_consumer_loop(),
        heartbeat_monitor(),
        housekeeping_loop(),
        monitor_containers(),
    )


def main() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Session Manager stopped")


if __name__ == "__main__":
    main()
