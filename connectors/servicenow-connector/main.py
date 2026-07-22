"""ServiceNow Connector — polls for new incidents and creates investigation tasks.

Polls the ServiceNow REST API (Table API) for new incidents matching a filter
query. Each new incident becomes a task in the Postgres task queue targeting the
incident-investigator workflow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

import httpx

from shared.lib.platform_secrets import apply_platform_env_defaults, load_connector_instance

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
    return (
        os.environ.get("PLATFORM_CONFIG_FILE") or os.environ.get("PLATFORM_SECRETS_FILE") or "/app/platform-config.yaml"
    )


def _bootstrap_platform_env() -> None:
    identity = os.environ.get("AGE_IDENTITY", "") or None
    loaded = apply_platform_env_defaults(os.environ, path=_platform_config_file(), identity=identity)
    if loaded:
        logger.info("Loaded %s platform config entries from %s", len(loaded), _platform_config_file())


def _load_instance_config() -> dict:
    instance_id = os.environ.get("CONNECTOR_INSTANCE_ID", "").strip()
    if not instance_id:
        raise RuntimeError("CONNECTOR_INSTANCE_ID must be set to a connectors.instances entry in platform config")
    config = load_connector_instance(_platform_config_file(), instance_id)
    if not config:
        raise RuntimeError(f"Connector instance {instance_id!r} not found in {_platform_config_file()}")
    return config


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


def _parse_incident(record: dict, config: dict) -> dict[str, Any]:
    """Parse a ServiceNow incident record using the instance parsing.extract config."""
    extract_config = config.get("parsing", {}).get("extract", {})
    parsed: dict[str, Any] = {}
    for field, path in extract_config.items():
        parsed[field] = _extract_nested(record, path)
    parsed["raw_record"] = json.dumps(record)[:5000]
    return parsed


def _build_prompt(parsed: dict) -> str:
    """Build the investigation prompt for the incident-investigator workflow."""
    incident_id = parsed.get("incident_id", "unknown")
    severity = parsed.get("severity", "unknown")
    service = parsed.get("service", "unknown")
    customer = parsed.get("customer", "unknown")
    description = parsed.get("description", "")
    subscription_id = parsed.get("subscription_id", "")
    order_id = parsed.get("order_id", "")
    customer_id = parsed.get("customer_id", "")
    error_code = parsed.get("error_code", "")

    prompt = (
        f"Investigate ServiceNow incident {incident_id}.\n\n"
        f"Priority: {severity}\n"
        f"Service: {service}\n"
        f"Customer: {customer}\n"
    )
    if subscription_id:
        prompt += f"Subscription ID: {subscription_id}\n"
    if order_id:
        prompt += f"Order ID: {order_id}\n"
    if customer_id:
        prompt += f"Customer/Business ID: {customer_id}\n"
    if error_code:
        prompt += f"Error Code: {error_code}\n"
    prompt += (
        f"\nDescription:\n{description}\n\n"
        f"Triage this incident: extract identifiers, classify whether log analysis "
        f"and/or CRM lookup are needed, check Hindsight for similar past incidents, "
        f"run the appropriate analysis, and produce an RCA with confidence score."
    )
    return prompt


async def _create_task_from_incident(parsed: dict, config: dict) -> None:
    """Create a task in Postgres from a parsed ServiceNow incident."""
    from shared.lib.db import async_session_factory
    from shared.lib.task_queue import create_task

    target = config.get("target", {})
    coalescing = config.get("coalescing", {})

    coalesce_key = None
    coalesce_window = 300
    if coalescing.get("enabled"):
        key_field = coalescing.get("key_field", "incident_id")
        coalesce_key = f"{target.get('workflow', 'incident-investigator')}:{parsed.get(key_field, 'unknown')}"
        coalesce_window = coalescing.get("window_sec", 300)

    prompt = _build_prompt(parsed)

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=target.get("workflow", "incident-investigator"),
            prompt=prompt,
            channel="servicenow",
            metadata={
                "source": "servicenow-connector",
                "incident_id": parsed.get("incident_id"),
                "severity": parsed.get("severity"),
                "service": parsed.get("service"),
                "customer": parsed.get("customer"),
                "subscription_id": parsed.get("subscription_id"),
                "order_id": parsed.get("order_id"),
                "customer_id": parsed.get("customer_id"),
                "error_code": parsed.get("error_code"),
                "description": parsed.get("description"),
                "raw_record": parsed.get("raw_record", ""),
            },
            coalesce_key=coalesce_key,
            coalesce_window_sec=coalesce_window,
        )
        logger.info(
            "Created task %s from ServiceNow incident %s (P%s, service=%s)",
            task.id,
            parsed.get("incident_id"),
            parsed.get("severity"),
            parsed.get("service"),
        )


async def run_polling_consumer(config: dict) -> None:
    """Poll ServiceNow Table API for new incidents."""
    source = config.get("source", {})
    instance_url = source.get("instance_url") or os.environ.get("SERVICENOW_INSTANCE_URL", "")
    table = source.get("table", "incident")
    query = source.get("query", "state=1^priority<=3")
    fields = source.get("fields", [])
    interval = source.get("interval_sec", 60)

    username = os.environ.get("SERVICENOW_USERNAME", "")
    password = os.environ.get("SERVICENOW_PASSWORD", "")

    if not instance_url:
        logger.error("SERVICENOW_INSTANCE_URL must be configured")
        return
    if not username or not password:
        logger.error("SERVICENOW_USERNAME and SERVICENOW_PASSWORD must be configured")
        return

    # Track already-processed incidents to avoid duplicates
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

                    parsed = _parse_incident(record, config)
                    await _create_task_from_incident(parsed, config)
                    new_count += 1

                if new_count:
                    logger.info("Processed %d new incidents from ServiceNow", new_count)

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
