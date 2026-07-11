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
_plugin_worker_cache: list[tuple[str, int]] = []
_plugin_worker_cache_loaded_at = 0.0
_PLUGIN_SCAN_INTERVAL_SEC = 30.0


def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down gracefully", sig)
    _shutdown = True


def _refresh_plugin_worker_cache(force: bool = False) -> list[tuple[str, int]]:
    """Refresh workflow concurrency limits at a bounded cadence."""
    global _plugin_worker_cache, _plugin_worker_cache_loaded_at

    now = time.monotonic()
    if not force and _plugin_worker_cache and now - _plugin_worker_cache_loaded_at < _PLUGIN_SCAN_INTERVAL_SEC:
        return _plugin_worker_cache

    plugins: list[tuple[str, int]] = []
    for workflow, config in discover_all_plugin_configs():
        max_workers = config.get("runtime", {}).get("parallel_workers", 1)
        plugins.append((workflow, max_workers))

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

                task = None
                for workflow, max_workers in plugins:
                    workflow_state = agent_state.get(workflow)
                    if workflow_state and (not workflow_state["provisioned"] or workflow_state["paused"]):
                        continue
                    task = await dequeue_task(session, workflow=workflow, max_running=max_workers)
                    if task:
                        break

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
