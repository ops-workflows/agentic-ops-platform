"""Container Lifecycle — Docker API for agent session containers.

Spawns a Docker container per main agent task. Subagents run INSIDE the
same container (Agent SDK handles this). Monitors container exit, updates
task status, posts final summary to the configured message provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from session_manager.memory_sync import backup_memory
from session_manager.runtime_launchers import (
    DockerRuntimeLauncher,
    RuntimeHandle,
    RuntimeLaunchSpec,
    get_runtime_launcher,
)
from session_manager.workflow_config import load_agent_yaml, workflow_package_path

from shared.lib.config import settings
from shared.lib.crypto import decrypt_agent_secrets
from shared.lib.db import async_session_factory
from shared.lib.message_bus import post_channel_message
from shared.lib.models import Session, SessionEvent, Task
from shared.lib.platform_secrets import load_platform_env, load_platform_runtime_env
from shared.lib.task_queue import complete_task
from shared.lib.workflow_bundles import WorkflowRepoMetadata, build_workflow_bundle
from shared.lib.workflow_paths import configured_workflow_roots

logger = logging.getLogger(__name__)

# Track running containers: task_id → container_id
_running_containers: dict[str, str] = {}


def has_live_runtime(task_id: str, *, container_id: str | None = None) -> bool:
    """Return whether a live runtime already exists for this task."""
    launcher = get_runtime_launcher()
    for runtime_status in launcher.list_sessions():
        if runtime_status.task_id != task_id:
            continue
        if container_id and runtime_status.id != container_id:
            continue
        if runtime_status.status == "running":
            return True
    return False


def _latest_event(events: Iterable[SessionEvent], *event_types: str) -> SessionEvent | None:
    wanted = set(event_types)
    matches = [event for event in events if event.event_type in wanted]
    if not matches:
        return None
    return max(matches, key=lambda event: event.timestamp)


def _get_agent_env_vars(agent_config: dict) -> dict[str, str]:
    """Return non-secret agent env vars for header expansion in the harness."""
    env_config = agent_config.get("env", {})
    if not isinstance(env_config, dict):
        logger.warning("Ignoring non-mapping env section in agent.yaml")
        return {}

    env_vars: dict[str, str] = {}
    for env_name, value in env_config.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            env_vars[str(env_name)] = str(value)
            continue
        logger.warning("Ignoring non-scalar env value %s in agent.yaml", env_name)
    return env_vars


def _apply_runtime_env_overrides(environment: dict[str, str], overrides: dict[str, str | None]) -> None:
    """Apply platform runtime env values, allowing null to remove a variable."""
    for env_name, value in overrides.items():
        if value is None:
            environment.pop(env_name, None)
            continue
        environment[env_name] = value


def _get_session_model_selector(agent_config: dict) -> str | None:
    """Return the workflow's model selector or direct model override."""
    session_config = agent_config.get("session", {})
    if not isinstance(session_config, dict):
        return None
    model = session_config.get("model")
    if model is None:
        return None
    selector = str(model).strip()
    return selector or None


def _active_platform_config_file(release: dict | None = None) -> str:
    """Return the config snapshot activated by Workflow Repo Sync when available."""
    release = release if release is not None else _load_active_release()
    if release:
        config_key = str(release.get("platform_config_key") or "")
        bucket = settings.runtime_bundle_object_store_bucket.strip()
        if config_key and bucket:
            from shared.lib.object_store import download_bytes

            contents = download_bytes(bucket, config_key)
            if contents is not None:
                cache_root = Path(settings.runtime_bundle_root or tempfile.gettempdir()).expanduser() / ".release-cache"
                cache_root.mkdir(parents=True, exist_ok=True)
                config_path = cache_root / f"platform-config-{release['release_id']}.yaml"
                config_path.write_bytes(contents)
                return str(config_path)

    bundle_root = settings.runtime_bundle_root.strip()
    if bundle_root:
        snapshot = Path(bundle_root).expanduser() / "platform-config.yaml"
        if snapshot.is_file():
            return str(snapshot)
    return settings.platform_config_file or settings.platform_secrets_file


def _load_active_release() -> dict | None:
    """Load the object-store release pointer once before a task is launched."""
    bucket = settings.runtime_bundle_object_store_bucket.strip()
    if not bucket:
        return None
    from shared.lib.object_store import download_bytes

    pointer = download_bytes(bucket, "releases/active.json")
    if pointer is None:
        return None
    try:
        manifest_key = str(json.loads(pointer).get("manifest_key") or "")
        manifest_bytes = download_bytes(bucket, manifest_key) if manifest_key else None
        manifest = json.loads(manifest_bytes) if manifest_bytes else None
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Ignoring invalid active workflow release pointer")
        return None
    return manifest if isinstance(manifest, dict) and manifest.get("release_id") else None


def _get_platform_runtime_env(
    model_selector: str | None = None, *, platform_file: str | None = None
) -> dict[str, str | None]:
    """Load platform-wide runtime env overrides for every session container."""
    platform_file = platform_file or _active_platform_config_file()
    if not platform_file:
        return {}
    return load_platform_runtime_env(platform_file, model_selector=model_selector)


def _get_platform_config_env(*, platform_file: str | None = None) -> dict[str, str]:
    """Load plain platform config values for runtime-container service endpoints."""
    platform_file = platform_file or _active_platform_config_file()
    if not platform_file:
        return {}
    return load_platform_env(platform_file, identity=settings.age_identity)


def _get_host_repo_root() -> str:
    if settings.host_repo_root:
        return settings.host_repo_root.rstrip("/")
    return os.path.dirname(settings.workflow_root.rstrip("/"))


def _resolve_plugin_mount(workflow: str) -> str | None:
    """Return the host-visible workflow path to bind into runtime containers."""
    host_workflow_dir = os.path.join(_get_host_repo_root(), "workflows", workflow)
    if settings.host_repo_root:
        return host_workflow_dir

    resolved_workflow_dir = workflow_package_path(workflow)
    if resolved_workflow_dir and os.path.isdir(resolved_workflow_dir):
        return str(resolved_workflow_dir)

    return host_workflow_dir if os.path.isdir(host_workflow_dir) else None


def _runtime_bundle_checksum(bundle_dir: Path) -> str:
    manifest = bundle_dir / "manifest.yaml"
    if not manifest.exists():
        return ""
    return "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()


def _format_bundle_uri(workflow: str) -> str:
    template = settings.runtime_bundle_uri_template.strip()
    if not template:
        return ""
    return template.format(workflow=workflow)


def _resolve_workflow_bundle(workflow: str, release: dict | None = None) -> tuple[str | None, str | None, str | None]:
    """Return local bundle path, bundle URI, and checksum for a workflow if configured."""
    if release:
        bundle = (release.get("bundles") or {}).get(workflow)
        bucket = settings.runtime_bundle_object_store_bucket.strip()
        if isinstance(bundle, dict) and bucket and bundle.get("key"):
            from shared.lib.object_store import presigned_get_url

            return (
                None,
                presigned_get_url(
                    bucket,
                    str(bundle["key"]),
                    expires_sec=settings.runtime_bundle_presigned_url_expires_sec,
                ),
                str(bundle.get("checksum") or "") or None,
            )
    bundle_uri = _format_bundle_uri(workflow) or None
    bundle_root = settings.runtime_bundle_root.strip()
    if not bundle_root:
        return None, bundle_uri, None

    output_dir = Path(bundle_root).expanduser()
    bundle_dir = output_dir / workflow
    freshly_built = not bundle_dir.exists()
    if freshly_built:
        platform_root = Path(__file__).resolve().parents[1]
        build_workflow_bundle(
            workflow=workflow,
            output_dir=output_dir,
            platform_root=platform_root,
            workflow_roots=configured_workflow_roots(),
            repo_metadata=WorkflowRepoMetadata(
                name=settings.workflow_repo_url or "local",
                url=settings.workflow_repo_url,
                ref=settings.workflow_repo_ref,
            ),
        )

    if not bundle_dir.exists():
        raise FileNotFoundError(f"Workflow bundle was not created: {bundle_dir}")

    object_store_bucket = settings.runtime_bundle_object_store_bucket.strip()
    if not bundle_uri and object_store_bucket:
        bundle_uri = _object_store_bundle_uri(
            bundle_dir, workflow, bucket=object_store_bucket, freshly_built=freshly_built
        )

    return str(bundle_dir), bundle_uri or f"file://{bundle_dir}", _runtime_bundle_checksum(bundle_dir)


def _object_store_bundle_uri(bundle_dir: Path, workflow: str, *, bucket: str, freshly_built: bool) -> str:
    """Upload (if needed) and return a short-lived presigned https URL for a bundle.

    The presigned URL keeps the runtime container free of any cloud SDK: it
    only needs a plain https download. Upload only happens once per fresh
    local bundle build; subsequent calls just re-sign the same object.
    """
    from shared.lib.object_store import presigned_get_url
    from shared.lib.workflow_bundles import upload_bundle_archive

    key = f"bundles/{workflow}.tar.gz"
    if freshly_built:
        key = upload_bundle_archive(bundle_dir, workflow, bucket=bucket)
    return presigned_get_url(bucket, key, expires_sec=settings.runtime_bundle_presigned_url_expires_sec)


def _cleanup_stale_container_name(client, container_name: str) -> None:
    """Remove an exited or never-started container that blocks a retry by name."""
    DockerRuntimeLauncher(client)._cleanup_stale_container_name(container_name)


async def spawn_agent_session(task: Task) -> RuntimeHandle | None:
    """Spawn a runtime execution for an agent session.

    Each main agent task gets a new runtime. Subagents run inside
    the same container (Agent SDK handles subagent orchestration).
    """
    try:
        agent_config = load_agent_yaml(task.workflow)
        task_metadata = task.task_metadata if isinstance(task.task_metadata, dict) else {}
        runtime = agent_config.get("runtime", {})
        legacy_reminder = runtime.get("ask_user_question_reminder", {})
        if not isinstance(legacy_reminder, dict):
            legacy_reminder = {}
        reminder_config = legacy_reminder
        container_image = runtime.get("container_image", "ai-ops-agent-runtime:latest")
        release = _load_active_release()
        platform_file = _active_platform_config_file(release)
        platform_config_env = _get_platform_config_env(platform_file=platform_file)

        environment = {
            # ── Task context (non-secret) ──
            "TASK_ID": str(task.id),
            "TASK_PROMPT": task.prompt,
            "TASK_WORKFLOW": task.workflow,
            "MAX_TURNS": str(agent_config.get("session", {}).get("max_turns", 50)),
            "RUNTIME_TIMEOUT_SEC": str(runtime.get("runtime_timeout_sec", 300)),
            "OPERATOR_APPROVAL_TIMEOUT_SEC": str(runtime.get("approval_timeout_sec", 3600)),
            "ASK_USER_QUESTION_REMINDER_ENABLED": str(reminder_config.get("enabled", bool(reminder_config))).lower(),
            "ASK_USER_QUESTION_REMINDER_MIN_TURNS": str(reminder_config.get("min_turns", 10)),
            "ASK_USER_QUESTION_REMINDER_TURN_RATIO": str(reminder_config.get("turn_budget_ratio", 0.7)),
            "ASK_USER_QUESTION_REMINDER_TIME_RATIO": str(reminder_config.get("time_budget_ratio", 0.75)),
            "ASK_USER_QUESTION_REMINDER_RECENT_QUESTION_TURN_WINDOW": str(
                reminder_config.get("recent_question_turn_window", 8)
            ),
            # ── Routing context for MCP ${VAR} header expansion (non-secret) ──
            "MESSAGE_CHANNEL": task.message_channel or "",
            "MESSAGE_CHANNEL_ID": str(task_metadata.get("channel_id") or ""),
            "MESSAGE_THREAD_ID": task.message_thread or "",
            "MESSAGE_TEAM_ID": str(task_metadata.get("team_id") or ""),
            "MESSAGE_TEAM_NAME": str(
                task_metadata.get("team_domain")
                or task_metadata.get("team_name")
                or settings.message_bus_team_name
                or ""
            ),
            # ── Harness-only message provider context for approvals/questions ──
            "MESSAGE_BUS_BOT_TOKEN": settings.message_bus_bot_token or "",
            "MESSAGE_BUS_API_URL": platform_config_env.get("MESSAGE_BUS_API_URL", settings.message_bus_api_url),
            "MESSAGE_BUS_PROVIDER": platform_config_env.get("MESSAGE_BUS_PROVIDER", settings.message_bus_provider),
            "CONTROL_PLANE_UI_URL": platform_config_env.get("CONTROL_PLANE_UI_URL", settings.control_plane_ui_url),
            # ── Observability (non-secret) ──
            "EVENT_COLLECTOR_URL": settings.gateway_event_url,
            "GATEWAY_EVENT_URL": settings.gateway_event_url,
            "HINDSIGHT_URL": settings.hindsight_url,
            # NOTE: Per-agent secrets are injected here so Claude Code can expand
            # ${VAR} placeholders in .mcp.json. Bash containment comes from the
            # plugin's .claude/settings.json sandbox configuration.
        }

        model_selector = _get_session_model_selector(agent_config)
        _apply_runtime_env_overrides(
            environment,
            _get_platform_runtime_env(model_selector=model_selector, platform_file=platform_file),
        )
        environment.update(_get_agent_env_vars(agent_config))

        # ── Decrypt agent secrets → env vars ──────────────────────
        # Secrets section in agent.yaml holds ENC[age,...] values.
        # Session manager decrypts using its age identity and injects cleartext
        # as container env vars for the runtime harness and Claude Code.
        secrets = agent_config.get("secrets", {}) if isinstance(agent_config, dict) else {}
        has_plain_secret = any(
            isinstance(spec, dict) and str(spec.get("encrypted") or "").startswith("ENC[plain,")
            for spec in (secrets.values() if isinstance(secrets, dict) else [])
        )
        if settings.age_identity or has_plain_secret:
            decrypted = decrypt_agent_secrets(agent_config, identity=settings.age_identity)
            environment.update(decrypted)

        # Build runtime mount inputs. Docker consumes these as volumes; Cloud
        # Run/Kubernetes launchers consume the same spec as deployment metadata.
        # Mount the workflow directory READ-ONLY — the runtime stages a
        # writable workspace copy under /workspace at startup so the
        # host checkout is never mutated by session side-effects.
        host_repo_root = _get_host_repo_root()
        workflow_mount = _resolve_plugin_mount(task.workflow)

        shared_dir = os.path.join(host_repo_root, "shared")
        shared_mount = shared_dir if settings.host_repo_root or os.path.isdir(shared_dir) else None
        bundle_path, bundle_uri, bundle_checksum = _resolve_workflow_bundle(task.workflow, release=release)

        # Per-agent memory mounted separately — the runtime symlinks it
        # into the workspace at .claude/agent-memory during staging.
        memory_vol_name = f"agent-memory-{task.workflow}"

        container_name = f"session-{str(task.id)[:8]}-{task.workflow}"

        handle = get_runtime_launcher().launch(
            RuntimeLaunchSpec(
                task_id=str(task.id),
                workflow=task.workflow,
                image=container_image,
                environment=environment,
                plugin_dir=None if bundle_path or bundle_uri else workflow_mount,
                # shared/lib is always baked into the runtime image (see
                # runtime/Dockerfile); this optional host bind-mount only
                # overrides it for local development, and only makes sense
                # when there is no local bundle_path occupying the mount
                # namespace the launcher would otherwise use for it.
                shared_dir=None if bundle_path else shared_mount,
                workflow_bundle_path=bundle_path,
                workflow_bundle_uri=bundle_uri,
                workflow_bundle_checksum=bundle_checksum,
                memory_volume_name=memory_vol_name,
                container_name=container_name,
                timeout_sec=int(runtime.get("runtime_timeout_sec", 300) or 300),
            )
        )

        # Track running runtime
        _running_containers[str(task.id)] = handle.id

        # Update task with container_id and create session record
        async with async_session_factory() as session:
            # Update task
            from sqlalchemy import update

            await session.execute(
                update(Task).where(Task.id == task.id).values(container_id=handle.id, session_id=handle.short_id)
            )

            # Create session record
            db_session = Session(
                task_id=task.id,
                container_id=handle.id,
                status="running",
                started=datetime.now(UTC),
            )
            session.add(db_session)
            await session.commit()

        logger.info("Spawned runtime %s (%s) for task %s", handle.short_id, handle.provider, task.id)
        return handle

    except Exception:
        logger.exception("Failed to spawn runtime for task %s", task.id)
        async with async_session_factory() as session:
            await complete_task(session, task.id, status="failed", error="Failed to spawn runtime")
        return None


async def monitor_containers() -> None:
    """Monitor running containers for completion.

    Runs as a background coroutine — checks container status periodically.

    Completion routing model (uniform for all task sources):
      1. Session manager updates task status in Postgres (exit 0 → succeeded, else → failed)
    2. Session manager posts completion/failure to the message channel (if configured)
      3. Session manager backs up agent memory to MinIO
            4. Runtime streams telemetry during the session; plugin hooks may retain hindsight or trigger reflection

    Mid-flight communication (approvals, progress, RCA summaries) is handled by the
    agent itself via explicit MCP tool calls (mcp_message.post_message, etc.).
    """
    logger.info("Container monitor started")

    while True:
        try:
            launcher = get_runtime_launcher()

            for runtime_status in launcher.list_sessions():
                task_id = runtime_status.task_id
                workflow = runtime_status.workflow

                if runtime_status.status == "exited":
                    exit_code = runtime_status.exit_code if runtime_status.exit_code is not None else -1
                    logger.info("Runtime %s exited (code=%d, task=%s)", runtime_status.short_id, exit_code, task_id)

                    # Collect logs
                    logs = runtime_status.logs

                    final_status = "succeeded" if exit_code == 0 else "failed"
                    error_msg = None if exit_code == 0 else f"Container exited with code {exit_code}\n{logs[-500:]}"

                    try:
                        task_uuid = uuid.UUID(task_id)
                        async with async_session_factory() as session:
                            lifecycle_data: dict | None = None
                            error_msg = None
                            duration_sec = None
                            final_status = "succeeded" if exit_code == 0 else "failed"
                            input_tok = 0
                            output_tok = 0
                            turns = 0

                            # Read the latest session_complete payload first so we can
                            # persist the final result text onto the task record.
                            from sqlalchemy import select, update

                            session_result = await session.execute(
                                select(Session).where(Session.task_id == task_uuid).limit(1)
                            )
                            db_session = session_result.scalar_one_or_none()

                            result = await session.execute(
                                select(SessionEvent)
                                .where(
                                    SessionEvent.task_id == task_uuid,
                                    SessionEvent.event_type.in_(
                                        ["session_complete", "session_error", "session_timeout"]
                                    ),
                                )
                                .order_by(SessionEvent.timestamp.desc())
                            )
                            lifecycle_events = list(result.scalars().all())
                            completion_event = _latest_event(lifecycle_events, "session_complete")
                            error_event = _latest_event(lifecycle_events, "session_error", "session_timeout")

                            if completion_event and completion_event.data:
                                lifecycle_data = completion_event.data
                            elif error_event and error_event.data:
                                lifecycle_data = error_event.data

                            if lifecycle_data:
                                input_tok = int(lifecycle_data.get("input_tokens", 0) or 0)
                                output_tok = int(lifecycle_data.get("output_tokens", 0) or 0)
                                turns = int(lifecycle_data.get("turns", 0) or 0)

                            if db_session and db_session.started:
                                ended_at = datetime.now(UTC)
                                duration_sec = (ended_at - db_session.started).total_seconds()
                            else:
                                ended_at = datetime.now(UTC)

                            if error_event and error_event.data:
                                final_status = "timed_out" if error_event.event_type == "session_timeout" else "failed"
                                error_msg = str(error_event.data.get("error") or error_event.event_type)
                            elif exit_code != 0:
                                error_msg = f"Container exited with code {exit_code}\n{logs[-500:]}"

                            await complete_task(
                                session,
                                task_uuid,
                                status=final_status,
                                result=lifecycle_data,
                                tokens_used=input_tok + output_tok,
                                duration_sec=duration_sec,
                                error=error_msg,
                            )

                            # Update session record
                            await session.execute(
                                update(Session)
                                .where(Session.task_id == task_uuid)
                                .values(
                                    status=final_status,
                                    ended=ended_at,
                                    duration_sec=duration_sec,
                                    error=error_msg,
                                )
                            )
                            if lifecycle_data:
                                # Update Task.tokens_used (aggregate)
                                await session.execute(
                                    update(Task)
                                    .where(Task.id == task_uuid)
                                    .values(
                                        tokens_used=input_tok + output_tok,
                                        duration_sec=duration_sec,
                                        result=lifecycle_data,
                                    )
                                )
                                # Update Session with granular breakdown
                                await session.execute(
                                    update(Session)
                                    .where(Session.task_id == task_uuid)
                                    .values(
                                        tokens_input=input_tok,
                                        tokens_output=output_tok,
                                        turns=turns,
                                    )
                                )

                            await session.commit()
                    except Exception:
                        logger.exception("Failed to update task %s", task_id)

                    # Post completion to the originating message thread
                    await _post_completion_to_message_thread(task_id, workflow, final_status, error_msg)

                    # Backup memory to MinIO
                    await backup_memory(workflow)

                    # Remove runtime handle / local container
                    try:
                        launcher.cleanup_session(runtime_status)
                        _running_containers.pop(task_id, None)
                    except Exception:
                        logger.warning("Failed to cleanup runtime %s", runtime_status.short_id)

        except Exception:
            logger.exception("Error in container monitor")

        await asyncio.sleep(5)


async def _post_completion_to_message_thread(task_id: str, workflow: str, status: str, error: str | None) -> None:
    """Post task completion notification to the configured message provider.

    This is the UNIFORM completion path for ALL task sources:
    - Message-initiated: replies in the original thread
    - Alert / scheduled / API: posts to the task's configured message_channel
    - No message_channel set: silently skips (task only visible in Control Plane UI)

    Mid-flight approvals and clarifying questions are handled by the runtime
    harness via message thread replies. This function only handles the final
    completion/failure notification.
    """
    if not settings.message_bus_api_url:
        return

    # Look up message channel/thread from task
    try:
        task_uuid = uuid.UUID(task_id)
        async with async_session_factory() as session:
            from shared.lib.task_queue import get_task

            task = await get_task(session, task_uuid)
    except Exception:
        return

    if not task or not task.message_channel:
        return

    bot_token = settings.message_bus_bot_token
    if not bot_token:
        logger.warning(
            "Skipping message completion post for %s: platform MESSAGE_BUS_BOT_TOKEN not configured", workflow
        )
        return

    # Fetch session data for token/duration metadata
    duration_sec = None
    tokens_input = 0
    tokens_output = 0
    try:
        async with async_session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(Session).where(Session.task_id == task.id).limit(1))
            db_session = result.scalar_one_or_none()
            if db_session:
                if db_session.started and db_session.ended:
                    duration_sec = (db_session.ended - db_session.started).total_seconds()
                tokens_input = db_session.tokens_input or 0
                tokens_output = db_session.tokens_output or 0
    except Exception:
        logger.debug("Unable to load completion metadata for task %s", task_id, exc_info=True)

    if status == "succeeded":
        result_text = ""
        if task.result and isinstance(task.result, dict):
            result_text = str(task.result.get("result", "") or "").strip()

        text = result_text[:6000] if result_text else f":white_check_mark: Task `{task_id[:8]}` completed"
    else:
        text = f":x: Task `{task_id[:8]}` failed"
        if error:
            text += f"\n```\n{error[:300]}\n```"

    # Append platform metadata (duration + token usage)
    footer_parts = []
    if duration_sec is not None:
        if duration_sec < 60:
            footer_parts.append(f"**Duration**: {duration_sec:.0f}s")
        else:
            footer_parts.append(f"**Duration**: {duration_sec / 60:.1f}m")
    total_tokens = tokens_input + tokens_output
    if total_tokens > 0:
        footer_parts.append(f"**Tokens**: {total_tokens:,} ({tokens_input:,} in / {tokens_output:,} out)")
    if footer_parts:
        text += "\n\n_" + " | ".join(footer_parts) + "_"

    metadata = task.task_metadata if isinstance(task.task_metadata, dict) else {}

    team_name = str(metadata.get("team_domain") or metadata.get("team_name") or settings.message_bus_team_name or "")
    posted = await post_channel_message(
        settings.message_bus_provider,
        api_url=settings.message_bus_api_url,
        bot_token=bot_token,
        text=text,
        channel_id=str(metadata.get("channel_id") or ""),
        channel_name=task.message_channel or "",
        team_id=str(metadata.get("team_id") or ""),
        team_name=team_name,
        thread_root=task.message_thread or "",
    )
    if posted is None:
        logger.warning("Failed to post completion to message provider for task %s", task_id)
