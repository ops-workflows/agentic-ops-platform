"""SQLAlchemy ORM models for task_queue and control_plane schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── Task Queue ───────────────────────────────────────────────────────


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_workflow", "workflow"),
        Index("idx_tasks_created", "created"),
        {"schema": "task_queue"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "queued",
            "running",
            "waiting_approval",
            "waiting_user_input",
            "resume_pending",
            "succeeded",
            "failed",
            "lost",
            "timed_out",
            name="task_status",
            schema="task_queue",
        ),
        nullable=False,
        default="queued",
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    task_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    channel: Mapped[str | None] = mapped_column(Text)
    message_channel: Mapped[str | None] = mapped_column(Text)
    message_thread: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    container_id: Mapped[str | None] = mapped_column(Text)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_reason: Mapped[str | None] = mapped_column(Text)
    wait_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    coalesce_key: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    events: Mapped[list[TaskEvent]] = relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan")
    approvals: Mapped[list[Approval]] = relationship("Approval", back_populates="task", cascade="all, delete-orphan")


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        Index("idx_task_events_task", "task_id", "created"),
        {"schema": "task_queue"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_queue.tasks.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    task: Mapped[Task] = relationship("Task", back_populates="events")


# ─── Control Plane ────────────────────────────────────────────────────


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = {"schema": "control_plane"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    config_hash: Mapped[str | None] = mapped_column(Text)
    repo_path: Mapped[str | None] = mapped_column(Text)
    provisioned: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    sessions: Mapped[list[Session]] = relationship("Session", back_populates="agent")
    schedules: Mapped[list[Schedule]] = relationship("Schedule", back_populates="agent", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_task", "task_id"),
        Index("idx_sessions_agent", "agent_id"),
        {"schema": "control_plane"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_queue.tasks.id", ondelete="SET NULL")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("control_plane.agents.id", ondelete="SET NULL")
    )
    container_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    ended: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Float)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tools_used: Mapped[list] = mapped_column(JSONB, default=list)
    subagents_used: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent: Mapped[Agent | None] = relationship("Agent", back_populates="sessions")


class SessionEvent(Base):
    __tablename__ = "session_events"
    __table_args__ = (
        Index("idx_session_events_task", "task_id", "timestamp"),
        {"schema": "control_plane"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_queue.tasks.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    data: Mapped[dict | None] = mapped_column(JSONB)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("idx_approvals_task", "task_id", "requested_at"),
        Index("idx_approvals_status", "status", "requested_at"),
        {"schema": "control_plane"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_queue.tasks.id", ondelete="CASCADE")
    )
    workflow: Mapped[str | None] = mapped_column(Text)
    approval_kind: Mapped[str] = mapped_column(Text, nullable=False, default="operator_approval")
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    request_preview: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    approval_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    task: Mapped[Task] = relationship("Task", back_populates="approvals")


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = {"schema": "control_plane"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("control_plane.agents.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    agent: Mapped[Agent] = relationship("Agent", back_populates="schedules")


class BackgroundJobRun(Base):
    __tablename__ = "background_job_runs"
    __table_args__ = (
        Index("idx_background_job_runs_job_type", "job_type", "started_at"),
        Index("idx_background_job_runs_started_at", "started_at"),
        {"schema": "control_plane"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)


class WorkflowRepoState(Base):
    """Singleton row tracking the connected workflow repo's pinned version and last sync.

    One platform instance targets exactly one workflow repo, so this table
    always has a single row (id=1). The bootstrap-owned repo URL/PAT are
    never stored here — only the operator-facing pinned ref and sync status.
    """

    __tablename__ = "workflow_repo_state"
    __table_args__ = {"schema": "control_plane"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    pinned_ref: Mapped[str | None] = mapped_column(Text)
    last_synced_ref: Mapped[str | None] = mapped_column(Text)
    last_synced_commit: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(Text)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    discovered_workflows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    bundle_errors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
