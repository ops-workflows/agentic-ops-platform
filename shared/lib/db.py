"""SQLAlchemy async engine and session factory."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.lib.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.dsn,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_schema_ready = False


async def ensure_runtime_schema() -> None:
    """Backfill additive schema changes for long-lived local databases."""
    global _schema_ready
    if _schema_ready:
        return

    async with engine.begin() as conn:
        for status in (
            "waiting_approval",
            "waiting_user_input",
            "resume_pending",
        ):
            await conn.execute(text(f"ALTER TYPE task_queue.task_status ADD VALUE IF NOT EXISTS '{status}'"))
        await conn.execute(
            text(
                "UPDATE task_queue.tasks "
                "SET status = 'timed_out', "
                "error = COALESCE(error, 'Deprecated model availability wait state removed') "
                "WHERE status::text = 'waiting_model_availability'"
            )
        )
        await conn.execute(text("ALTER TABLE task_queue.tasks ADD COLUMN IF NOT EXISTS wait_reason TEXT"))
        await conn.execute(text("ALTER TABLE task_queue.tasks ADD COLUMN IF NOT EXISTS wait_deadline TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE task_queue.tasks ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE control_plane.sessions ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"))
        await conn.execute(
            text("ALTER TABLE control_plane.session_events ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        )
        await conn.execute(text("ALTER TABLE control_plane.approvals ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON task_queue.tasks (archived_at)"))
        await conn.execute(
            text("ALTER TABLE control_plane.agents ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT FALSE")
        )
        await conn.execute(text("ALTER TABLE control_plane.session_events DROP COLUMN IF EXISTS artifact_key"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS control_plane.approvals ("
                "id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
                "task_id UUID NOT NULL REFERENCES task_queue.tasks(id) ON DELETE CASCADE, "
                "workflow TEXT, "
                "approval_kind TEXT NOT NULL DEFAULT 'operator_approval', "
                "tool_name TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "request_preview TEXT, "
                "reason TEXT, "
                "metadata JSONB NOT NULL DEFAULT '{}', "
                "requested_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "resolved_at TIMESTAMPTZ, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_approvals_task ON control_plane.approvals (task_id, requested_at DESC)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_approvals_status ON control_plane.approvals (status, requested_at DESC)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS control_plane.background_job_runs ("
                "id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
                "job_type TEXT NOT NULL, "
                "scope TEXT, "
                "status TEXT NOT NULL DEFAULT 'running', "
                "started_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "finished_at TIMESTAMPTZ, "
                "duration_sec FLOAT, "
                "summary JSONB NOT NULL DEFAULT '{}', "
                "warnings JSONB NOT NULL DEFAULT '[]', "
                "error TEXT"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_background_job_runs_job_type "
                "ON control_plane.background_job_runs (job_type, started_at DESC)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_background_job_runs_started_at "
                "ON control_plane.background_job_runs (started_at DESC)"
            )
        )

    _schema_ready = True
    logger.info("Ensured runtime schema compatibility")


async def get_session() -> AsyncSession:
    """Dependency for FastAPI — yields an async DB session."""
    async with async_session_factory() as session:
        yield session
