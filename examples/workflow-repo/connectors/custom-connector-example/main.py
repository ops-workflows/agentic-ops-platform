"""Example custom connector — copy this directory to start a new one.

Rename the module/type (e.g. "my-source"), replace the polling body with a
real read from your source, and adjust the parsing/coalescing behavior to
match your instance config shape. See connectors/README.md in this directory
for the remaining wiring steps (Dockerfile, deployment override,
connectors.instances).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_shutdown = False


def _signal_handler(sig, frame):  # noqa: ARG001
    global _shutdown
    _shutdown = True


def _platform_config_file() -> str:
    return (
        os.environ.get("PLATFORM_CONFIG_FILE") or os.environ.get("PLATFORM_SECRETS_FILE") or "/app/platform-config.yaml"
    )


def _bootstrap_platform_env() -> None:
    from shared.lib.platform_secrets import apply_platform_env_defaults

    identity = os.environ.get("AGE_IDENTITY", "") or None
    loaded = apply_platform_env_defaults(os.environ, path=_platform_config_file(), identity=identity)
    if loaded:
        logger.info("Loaded %s platform config entries from %s", len(loaded), _platform_config_file())


def _load_instance_config() -> dict[str, Any]:
    from shared.lib.platform_secrets import load_connector_instance

    instance_id = os.environ.get("CONNECTOR_INSTANCE_ID", "").strip()
    if not instance_id:
        raise RuntimeError("CONNECTOR_INSTANCE_ID must be set to a connectors.instances entry in platform config")
    config = load_connector_instance(_platform_config_file(), instance_id)
    if not config:
        raise RuntimeError(f"Connector instance {instance_id!r} not found in {_platform_config_file()}")
    return config


async def _create_task_from_record(record: dict[str, Any], config: dict[str, Any]) -> None:
    from shared.lib.db import async_session_factory
    from shared.lib.task_queue import create_task

    target = config.get("target", {}) if isinstance(config.get("target"), dict) else {}
    coalescing = config.get("coalescing", {}) if isinstance(config.get("coalescing"), dict) else {}

    coalesce_key = None
    if coalescing.get("enabled"):
        key_field = str(coalescing.get("key_field") or "id")
        coalesce_key = f"{target.get('workflow', 'example-workflow')}:{record.get(key_field, 'unknown')}"

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=str(target.get("workflow") or "example-workflow"),
            prompt=f"Process this event:\n\n{record}",
            channel=str(target.get("channel") or "custom-example"),
            metadata={"source": "custom-connector-example", "record": record},
            message_channel=str(target.get("message_channel") or "") or None,
            coalesce_key=coalesce_key,
            coalesce_window_sec=int(coalescing.get("window_sec") or 300),
        )
        logger.info("Created task %s from custom connector event", task.id)


async def run_polling_consumer(config: dict[str, Any]) -> None:
    source = config.get("source", {}) if isinstance(config.get("source"), dict) else {}
    interval_sec = int(source.get("interval_sec") or 60)

    logger.info("Starting custom connector poller (interval=%ss)", interval_sec)
    while not _shutdown:
        # Replace this with a real read from your source, e.g. an httpx
        # request or a client-library call, using values from `source`.
        records: list[dict[str, Any]] = []
        for record in records:
            try:
                await _create_task_from_record(record, config)
            except Exception:
                logger.exception("Failed to create task from record")
        await asyncio.sleep(interval_sec)


def main() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    _bootstrap_platform_env()
    config = _load_instance_config()
    asyncio.run(run_polling_consumer(config))


if __name__ == "__main__":
    main()
