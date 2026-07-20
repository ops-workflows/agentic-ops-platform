"""Shared configuration loaded from environment variables."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings

from shared.lib.platform_secrets import load_platform_env


class DatabaseSettings(BaseSettings):
    pg_host: str = "postgres"
    pg_port: int = 5432
    pg_db: str = "agentic_ops"
    pg_user: str = "agentic_ops"
    pg_password: str = ""

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"

    @property
    def sync_dsn(self) -> str:
        return f"postgresql+psycopg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


class ObjectStoreSettings(BaseSettings):
    # provider: "s3" (MinIO, AWS S3, or any S3-compatible endpoint) or "gcs"
    # (Google Cloud Storage). The same provider selection is used uniformly
    # for agent-memory backups and workflow bundles across every deployment
    # target (compose or kubernetes).
    object_store_provider: str = "s3"
    object_store_endpoint: str = "minio:9000"
    object_store_access_key: str = "agentic_ops"
    object_store_secret_key: str = ""
    object_store_secure: bool = False
    # gcs only — optional; the client can also infer the project from ADC.
    object_store_gcp_project: str = ""


class MessageBusSettings(BaseSettings):
    message_bus_provider: str = "mattermost"
    message_outgoing_webhook_secret: str = ""
    message_bus_bot_token: str = ""
    message_bus_api_url: str = ""
    message_bus_team_name: str = ""


class HindsightSettings(BaseSettings):
    hindsight_url: str = "http://hindsight:8888"


class Settings(
    DatabaseSettings,
    ObjectStoreSettings,
    MessageBusSettings,
    HindsightSettings,
):
    gateway_host: str = "0.0.0.0"  # noqa: S104
    gateway_port: int = 8080
    poll_interval_sec: int = 2
    workflow_root: str = "/app/workflows"
    repo_path: str = ""
    host_repo_root: str = ""
    gateway_event_url: str = "http://gateway:8080/events"
    gateway_public_base_url: str = ""
    control_plane_ui_url: str = ""
    platform_config_file: str = "/app/platform-config.yaml"
    platform_secrets_file: str = ""

    # ── Workflow repository loading ──────────────────────────────
    # workflow_repo_paths can contain multiple mounted workflow roots separated
    # by os.pathsep. Each root may be a workflow directory, a directory whose
    # direct children are workflows, or a repo root containing workflows/*.
    workflow_repo_paths: str = ""
    workflow_repo_source: str = ""
    workflow_repo_url: str = ""
    workflow_repo_ref: str = ""
    # Bootstrap-only credential to authenticate the clone/fetch of
    # workflow_repo_url. Never read from repo-owned platform-config.yaml
    # (that would be circular); comes only from the operator's generated
    # bootstrap env/secret (see `make bootstrap`).
    workflow_repo_pat: str = ""
    workflow_repo_local_path: str = "/workspace/workflows"

    # ── Runtime launcher and memory sync ─────────────────────────
    runtime_launcher: str = "docker"
    memory_sync_mode: str = "docker_volume"
    memory_filesystem_root: str = "/memory"
    runtime_bundle_root: str = ""
    runtime_bundle_uri_template: str = ""
    # When set, session-manager tars and uploads freshly built bundles to this
    # object-store bucket and hands the runtime a short-lived presigned https
    # URL, instead of (or in addition to) a static runtime_bundle_uri_template.
    runtime_bundle_object_store_bucket: str = ""
    runtime_bundle_presigned_url_expires_sec: int = 3600
    kubernetes_memory_helper_image: str = ""
    kubernetes_bootstrap_secret: str = ""
    housekeeping_enabled: bool = True
    housekeeping_interval_sec: int = 3600
    background_job_run_history_limit: int = 5
    task_archive_after_days: int = 14
    task_delete_after_days: int = 0
    learning_memory_retention_days: int = 30
    hindsight_request_retries: int = 3
    hindsight_request_retry_backoff_sec: float = 0.5
    agent_memory_versions_to_keep: int = 10
    agent_memory_retention_days: int = 90
    kubernetes_namespace: str = "default"

    # ── Secret management ─────────────────────────────────────────
    # Age public key — used by gateway API to encrypt new secrets.
    age_public_key: str = ""
    # Age identity (private key) — used by session manager to decrypt
    # agent secrets at container spawn time.  Can be an armored key
    # string or a file path prefixed with "file:".
    age_identity: str = ""


settings = Settings()


def _apply_platform_secret_defaults() -> None:
    """Overlay repo-stored platform config onto settings unless env explicitly set it."""
    platform_file = settings.platform_config_file or settings.platform_secrets_file
    if not platform_file:
        return

    loaded = load_platform_env(platform_file, identity=settings.age_identity or None)
    for env_var, value in loaded.items():
        field_name = env_var.lower()
        if not hasattr(settings, field_name):
            continue
        if os.environ.get(env_var):
            continue
        setattr(settings, field_name, value)


_apply_platform_secret_defaults()
