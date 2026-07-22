"""Generic GCP Pub/Sub Connector — creates workflow tasks from Pub/Sub messages."""

from __future__ import annotations

import asyncio
import atexit
import email
import json
import logging
import os
import re
import signal
import tempfile
from collections import UserDict
from email import policy
from email.message import Message
from html import unescape
from typing import Any

from shared.lib.platform_secrets import (
    apply_platform_env_defaults,
    load_connector_instance,
    load_enabled_connector_instance,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_shutdown = False


class _SafeFormatMap(UserDict):
    def __missing__(self, key: str) -> str:
        return ""


def _signal_handler(sig, frame):  # noqa: ARG001
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


def _load_instance_config() -> dict[str, Any]:
    instance_id = os.environ.get("CONNECTOR_INSTANCE_ID", "").strip()
    if instance_id:
        config = load_connector_instance(_platform_config_file(), instance_id)
    else:
        instance_id, config = load_enabled_connector_instance(_platform_config_file(), "gcp-pubsub")
    if not config:
        raise RuntimeError("Set CONNECTOR_INSTANCE_ID or enable exactly one gcp-pubsub instance in platform config")
    return config


def _configure_google_application_credentials() -> None:
    """Expose an encrypted platform-config service-account secret to Google ADC."""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    service_account_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if not service_account_json:
        return
    try:
        credentials = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        try:
            credentials = json.loads(service_account_json, strict=False)
        except json.JSONDecodeError:
            raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON must contain valid service-account JSON") from exc
        logger.warning("Normalizing literal control characters in GCP_SERVICE_ACCOUNT_JSON")
    if not isinstance(credentials, dict):
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON must contain a service-account JSON object")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as credentials_file:
        json.dump(credentials, credentials_file)
    credentials_path = credentials_file.name
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
    atexit.register(lambda: os.path.exists(credentials_path) and os.unlink(credentials_path))


def _extract_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _decode_payload(raw: bytes) -> tuple[dict[str, Any], str]:
    text = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"payload_text": text}, text
    if isinstance(value, dict):
        return value, json.dumps(value, ensure_ascii=False, indent=2)
    return {"payload": value}, json.dumps(value, ensure_ascii=False, indent=2)


def _extract_metadata(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    extract_config = config.get("parsing", {}).get("extract", {})
    metadata: dict[str, Any] = {}
    if isinstance(extract_config, dict):
        for key, path in extract_config.items():
            value = _extract_path(payload, str(path))
            if value is not None:
                metadata[str(key)] = value
    return metadata


def _trim_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _html_to_text(value: str, limit: int) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", value)
        return _trim_text(unescape(text), limit)
    return _trim_text(BeautifulSoup(value, "html.parser").get_text("\n", strip=True), limit)


def _email_body_text(message: Message, limit: int) -> str:
    html_body = ""
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = str(part.get_content())
        except (LookupError, UnicodeDecodeError):
            continue
        if content_type == "text/plain" and content.strip():
            return _trim_text(content, limit)
        if content_type == "text/html" and not html_body:
            html_body = content
    return _html_to_text(html_body, limit) if html_body else ""


def _parse_gcs_email(raw: bytes, metadata_key: str, max_body_chars: int) -> dict[str, Any]:
    """Extract RFC 822 headers and a readable MIME body from a GCS object."""
    message = email.message_from_bytes(raw, policy=policy.default)
    body_text = _email_body_text(message, max_body_chars)
    return {
        metadata_key: body_text,
        "email_subject": str(message.get("Subject", "")),
        "email_sender": str(message.get("From", "")),
        "email_recipient": str(message.get("To", "")),
        "email_date": str(message.get("Date", "")),
        "email_body_text": body_text,
    }


def _extract_email_metadata(metadata: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Apply optional deployment-provided regexes to parsed email headers and body."""
    parsing = config.get("parsing") if isinstance(config.get("parsing"), dict) else {}
    extract_config = parsing.get("email_extract") if isinstance(parsing.get("email_extract"), dict) else {}
    search_text = "\n".join(
        (
            f"Subject: {metadata.get('email_subject', '')}",
            f"From: {metadata.get('email_sender', '')}",
            f"To: {metadata.get('email_recipient', '')}",
            f"Date: {metadata.get('email_date', '')}",
            "",
            str(metadata.get("email_body_text", "")),
        )
    )
    extracted: dict[str, Any] = {}
    for field, pattern in extract_config.items():
        if pattern == "body":
            extracted[str(field)] = metadata.get("email_body_text", "")
            continue
        match = re.search(str(pattern), search_text, re.IGNORECASE | re.MULTILINE)
        extracted[str(field)] = match.group(1) if match else None
    return extracted


def _fetch_gcs_payload(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gcs_config = config.get("source", {}).get("gcs_payload") or {}
    if not isinstance(gcs_config, dict) or not gcs_config.get("enabled"):
        return {}

    bucket_name = _extract_path(payload, str(gcs_config.get("bucket_field") or "bucket"))
    object_name = _extract_path(payload, str(gcs_config.get("name_field") or "name"))
    if not bucket_name or not object_name:
        return {}

    from google.cloud import storage

    max_bytes = int(gcs_config.get("max_bytes") or 200_000)
    client = storage.Client()
    blob = client.bucket(str(bucket_name)).blob(str(object_name))
    raw = blob.download_as_bytes(start=0, end=max_bytes - 1)
    key = str(gcs_config.get("metadata_key") or "object_text")
    parser = str(gcs_config.get("parser") or "text").lower()
    if parser == "email":
        content = _parse_gcs_email(raw, key, int(gcs_config.get("max_body_chars") or 4_000))
    elif parser == "text":
        content = {key: raw.decode("utf-8", errors="replace")}
    else:
        raise ValueError(f"Unsupported gcs_payload parser: {parser}")
    return {
        **content,
        "gcs_bucket": str(bucket_name),
        "gcs_object": str(object_name),
        "gcs_uri": f"gs://{bucket_name}/{object_name}",
    }


def _render_prompt(template: str, values: dict[str, Any]) -> str:
    return template.format_map(
        _SafeFormatMap({key: "" if value is None else str(value) for key, value in values.items()})
    )


async def _create_task(
    payload: dict[str, Any],
    payload_text: str,
    attributes: dict[str, str],
    config: dict[str, Any],
) -> None:
    from shared.lib.db import async_session_factory
    from shared.lib.task_queue import create_task

    target = config.get("target", {}) if isinstance(config.get("target"), dict) else {}
    coalescing = config.get("coalescing", {}) if isinstance(config.get("coalescing"), dict) else {}
    metadata = _extract_metadata(payload, config)
    metadata.update(_fetch_gcs_payload(payload, config))
    metadata.update(_extract_email_metadata(metadata, config))
    metadata.update(
        {
            "source": "gcp-pubsub-connector",
            "pubsub_attributes": attributes,
            "payload": payload,
            "payload_text": payload_text[:5000],
        }
    )
    values = {**payload, **metadata, "payload_text": payload_text}
    prompt_template = str(target.get("prompt_template") or "Process this Pub/Sub event:\n\n{payload_text}")
    prompt = _render_prompt(prompt_template, values)

    coalesce_key = None
    if coalescing.get("enabled"):
        key_field = str(coalescing.get("key_field") or "event_id")
        coalesce_key = f"{target.get('workflow', 'example-workflow')}:{metadata.get(key_field, 'unknown')}"

    async with async_session_factory() as session:
        task = await create_task(
            session,
            workflow=str(target.get("workflow") or "example-workflow"),
            prompt=prompt,
            channel=str(target.get("channel") or "gcp-pubsub"),
            metadata=metadata,
            coalesce_key=coalesce_key,
            coalesce_window_sec=int(coalescing.get("window_sec") or 300),
        )
        logger.info("Created task %s from Pub/Sub message", task.id)


def _subscriber_callback(loop: asyncio.AbstractEventLoop, config: dict[str, Any]):
    def callback(message) -> None:
        payload, payload_text = _decode_payload(message.data)
        try:
            future = asyncio.run_coroutine_threadsafe(
                _create_task(payload, payload_text, dict(message.attributes or {}), config), loop
            )
            future.result()
        except Exception as exc:
            logger.exception("Failed to create task from Pub/Sub message %s: %s", message.message_id, exc)
            message.nack()
            return
        message.ack()

    return callback


async def run_subscriber(config: dict[str, Any]) -> None:
    from google.cloud import pubsub_v1

    source = config.get("source", {}) if isinstance(config.get("source"), dict) else {}
    subscription = str(source.get("subscription") or os.environ.get("GCP_PUBSUB_SUBSCRIPTION") or "").strip()
    if not subscription:
        logger.error("GCP_PUBSUB_SUBSCRIPTION or source.subscription must be configured")
        return

    project = str(source.get("project") or os.environ.get("GCP_PROJECT") or "").strip()
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = (
        subscription if subscription.startswith("projects/") else subscriber.subscription_path(project, subscription)
    )

    logger.info("Starting Pub/Sub subscriber: %s", subscription_path)
    future = subscriber.subscribe(subscription_path, callback=_subscriber_callback(asyncio.get_running_loop(), config))
    try:
        while not _shutdown:
            await asyncio.sleep(1)
    finally:
        future.cancel()
        subscriber.close()


def main() -> None:
    from shared.lib.health_server import start_health_server

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    start_health_server()
    _bootstrap_platform_env()
    _configure_google_application_credentials()
    config = _load_instance_config()
    asyncio.run(run_subscriber(config))


if __name__ == "__main__":
    main()
