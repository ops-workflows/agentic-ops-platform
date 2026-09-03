"""Generic ServiceNow Table API connector for creating workflow tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

import httpx

from shared.lib.crypto import decrypt_agent_secrets
from shared.lib.platform_secrets import (
    apply_platform_env_defaults,
    load_connector_instance,
    load_enabled_connector_instance,
)
from shared.lib.workflow_paths import find_workflow_package

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    _shutdown = True


def _platform_config_file() -> str:
    return os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml")


def _bootstrap_platform_env() -> None:
    identity = os.environ.get("AGE_IDENTITY", "") or None
    loaded = apply_platform_env_defaults(os.environ, path=_platform_config_file(), identity=identity)
    if loaded:
        logger.info("Loaded %s platform config entries from %s", len(loaded), _platform_config_file())


def _load_instance_config() -> dict:
    instance_id = os.environ.get("CONNECTOR_INSTANCE_ID", "").strip()
    if instance_id:
        config = load_connector_instance(_platform_config_file(), instance_id)
    else:
        instance_id, config = load_enabled_connector_instance(_platform_config_file(), "servicenow")
    if not config:
        raise RuntimeError("Set CONNECTOR_INSTANCE_ID or enable exactly one servicenow instance in platform config")
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    target = config.get("target") if isinstance(config.get("target"), dict) else {}
    if not str(source.get("table") or "").strip():
        raise RuntimeError("ServiceNow connector source.table must be configured")
    if not str(target.get("workflow") or "").strip():
        raise RuntimeError("ServiceNow connector target.workflow must be configured")
    if not str(target.get("prompt_template") or "").strip():
        raise RuntimeError("ServiceNow connector target.prompt_template must be configured")
    coalescing = config.get("coalescing") if isinstance(config.get("coalescing"), dict) else {}
    if coalescing.get("enabled") and not str(coalescing.get("key_field") or "").strip():
        raise RuntimeError("ServiceNow connector coalescing.key_field must be configured when coalescing is enabled")
    return {**config, "_instance_id": instance_id}


async def _connector_is_paused(connector_id: str) -> bool:
    from shared.lib.connector_state import connector_is_paused
    from shared.lib.db import async_session_factory

    async with async_session_factory() as session:
        return await connector_is_paused(session, connector_id)


def _load_target_workflow_env(config: dict) -> dict[str, str]:
    target = config.get("target") if isinstance(config.get("target"), dict) else {}
    workflow = str(target.get("workflow") or "").strip()
    if not workflow:
        raise RuntimeError("ServiceNow connector target.workflow must be configured")

    package = find_workflow_package(workflow)
    if package is None:
        raise RuntimeError(f"ServiceNow target workflow {workflow!r} was not found")

    workflow_env: dict[str, str] = {}
    configured_env = package.config.get("env")
    if isinstance(configured_env, dict):
        workflow_env.update(
            {
                str(key): str(value)
                for key, value in configured_env.items()
                if isinstance(value, (str, int, float, bool))
            }
        )

    secrets = package.config.get("secrets")
    if isinstance(secrets, dict) and secrets:
        workflow_env.update(
            decrypt_agent_secrets(
                package.config,
                identity=os.environ.get("AGE_IDENTITY", ""),
            )
        )
    return workflow_env


def _extract_nested(data: dict, dot_path: str) -> Any:
    """Extract a value from a nested dict using dot notation (e.g. 'caller_id.display_value')."""
    parts = dot_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _parse_record(record: dict, config: dict) -> dict[str, Any]:
    """Parse a ServiceNow record using the instance extraction mapping."""
    extract_config = config.get("parsing", {}).get("extract", {})
    parsed: dict[str, Any] = {}
    for field, path in extract_config.items():
        parsed[field] = _extract_nested(record, path)
    parsed["raw_record"] = json.dumps(record)[:5000]
    return parsed


class _SafeFormatMap(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _build_prompt(parsed: dict, config: dict) -> str:
    target = config.get("target") if isinstance(config.get("target"), dict) else {}
    template = str(target.get("prompt_template") or "")
    values = _SafeFormatMap({key: "" if value is None else str(value) for key, value in parsed.items()})
    return template.format_map(values)


async def _create_task_from_record(parsed: dict, config: dict) -> None:
    """Create a task in Postgres from a parsed ServiceNow record."""
    from shared.lib.db import async_session_factory
    from shared.lib.task_queue import create_task

    target = config.get("target", {})
    coalescing = config.get("coalescing", {})

    coalesce_key = None
    coalesce_window = 300
    if coalescing.get("enabled"):
        key_field = str(coalescing["key_field"])
        coalesce_key = f"{target['workflow']}:{parsed.get(key_field, 'unknown')}"
        coalesce_window = coalescing.get("window_sec", 300)

    prompt = _build_prompt(parsed, config)
    metadata = {**parsed, "source": "servicenow-connector"}

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=target["workflow"],
            prompt=prompt,
            channel=str(target.get("channel") or "servicenow"),
            metadata=metadata,
            coalesce_key=coalesce_key,
            coalesce_window_sec=coalesce_window,
        )
        logger.info("Created task %s from ServiceNow record", task.id)


async def run_polling_consumer(config: dict) -> None:
    """Poll the configured ServiceNow Table API for new records."""
    source = config.get("source", {})
    workflow_env = _load_target_workflow_env(config)
    instance_url = workflow_env.get("SERVICENOW_INSTANCE_URL", "")
    table = str(source["table"])
    query = str(source.get("query") or "")
    fields = source.get("fields", [])
    interval = source.get("interval_sec", 60)

    username = workflow_env.get("SERVICENOW_USERNAME", "")
    password = workflow_env.get("SERVICENOW_PASSWORD", "")

    if not instance_url:
        logger.error("SERVICENOW_INSTANCE_URL must be configured")
        return
    if not username or not password:
        logger.error("SERVICENOW_USERNAME and SERVICENOW_PASSWORD must be configured")
        return

    # Track already-processed records to avoid duplicates
    seen_sys_ids: set[str] = set()

    api_url = f"{instance_url.rstrip('/')}/api/now/table/{table}"
    params: dict[str, str] = {
        "sysparm_query": query,
        "sysparm_display_value": "true",
        "sysparm_limit": "50",
    }
    if fields:
        params["sysparm_fields"] = ",".join(fields)

    logger.info("Starting ServiceNow poller: %s (table=%s, interval=%ss)", instance_url, table, interval)

    async with httpx.AsyncClient(
        auth=(username, password),
        headers={"Accept": "application/json"},
        timeout=30.0,
    ) as client:
        while not _shutdown:
            if await _connector_is_paused(str(config.get("_instance_id") or "")):
                await asyncio.sleep(interval)
                continue
            try:
                resp = await client.get(api_url, params=params)
                resp.raise_for_status()
                data = resp.json()

                records = data.get("result", [])
                new_count = 0
                for record in records:
                    sys_id = record.get("sys_id", "")
                    if sys_id in seen_sys_ids:
                        continue
                    seen_sys_ids.add(sys_id)

                    parsed = _parse_record(record, config)
                    await _create_task_from_record(parsed, config)
                    new_count += 1

                if new_count:
                    logger.info("Processed %d new records from ServiceNow", new_count)

                # Prevent unbounded memory growth — keep only recent IDs
                if len(seen_sys_ids) > 10000:
                    seen_sys_ids.clear()

            except httpx.HTTPStatusError as e:
                logger.error("ServiceNow API error: %s %s", e.response.status_code, e.response.text[:200])
            except Exception:
                logger.exception("Error polling ServiceNow")

            await asyncio.sleep(interval)


async def main_async() -> None:
    from shared.lib.health_server import start_health_server

    start_health_server()
    _bootstrap_platform_env()
    config = _load_instance_config()
    logger.info("ServiceNow Connector starting (config: %s)", config.get("name", "unknown"))
    await run_polling_consumer(config)


def main() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("ServiceNow Connector stopped")


if __name__ == "__main__":
    main()
