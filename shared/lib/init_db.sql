-- Agentic Ops — Database Initialization
-- Creates schemas and tables for task queue, control plane, and hindsight

-- ─── Schemas ─────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS task_queue;
CREATE SCHEMA IF NOT EXISTS control_plane;
CREATE SCHEMA IF NOT EXISTS hindsight;

-- ─── Extensions ──────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Task Queue Schema ──────────────────────────────────────────────

CREATE TYPE task_queue.task_status AS ENUM (
    'queued', 'running', 'waiting_approval', 'waiting_user_input',
    'resume_pending', 'succeeded', 'failed',
    'lost', 'timed_out'
);

CREATE TABLE task_queue.tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow        TEXT NOT NULL,
    status          task_queue.task_status NOT NULL DEFAULT 'queued',
    prompt          TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    channel         TEXT,
    message_channel TEXT,
    message_thread  TEXT,
    session_id      TEXT,
    container_id    TEXT,
    heartbeat       TIMESTAMPTZ,
    wait_reason     TEXT,
    wait_deadline   TIMESTAMPTZ,
    result          JSONB,
    tokens_used     INT DEFAULT 0,
    duration_sec    FLOAT,
    error           TEXT,
    coalesce_key    TEXT,
    archived_at     TIMESTAMPTZ,
    created         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_status ON task_queue.tasks (status);
CREATE INDEX idx_tasks_workflow ON task_queue.tasks (workflow);
CREATE INDEX idx_tasks_coalesce ON task_queue.tasks (coalesce_key, status) WHERE coalesce_key IS NOT NULL;
CREATE INDEX idx_tasks_heartbeat ON task_queue.tasks (heartbeat) WHERE status = 'running';
CREATE INDEX idx_tasks_created ON task_queue.tasks (created DESC);
CREATE INDEX idx_tasks_archived_at ON task_queue.tasks (archived_at);

CREATE TABLE task_queue.task_events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id     UUID NOT NULL REFERENCES task_queue.tasks(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    data        JSONB,
    created     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_events_task ON task_queue.task_events (task_id, created);

-- ─── Control Plane Schema ───────────────────────────────────────────

CREATE TABLE control_plane.agents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT UNIQUE NOT NULL,
    description     TEXT,
    version         TEXT,
    config          JSONB NOT NULL DEFAULT '{}',
    config_hash     TEXT,
    repo_path       TEXT,
    provisioned     BOOLEAN DEFAULT FALSE,
    paused          BOOLEAN NOT NULL DEFAULT FALSE,
    provisioned_at  TIMESTAMPTZ,
    created         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE control_plane.sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID REFERENCES task_queue.tasks(id) ON DELETE SET NULL,
    agent_id        UUID REFERENCES control_plane.agents(id) ON DELETE SET NULL,
    container_id    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    started         TIMESTAMPTZ DEFAULT now(),
    ended           TIMESTAMPTZ,
    duration_sec    FLOAT,
    tokens_input    INT DEFAULT 0,
    tokens_output   INT DEFAULT 0,
    turns           INT DEFAULT 0,
    tools_used      JSONB DEFAULT '[]',
    subagents_used  JSONB DEFAULT '[]',
    error           TEXT,
    archived_at     TIMESTAMPTZ
);

CREATE INDEX idx_sessions_task ON control_plane.sessions (task_id);
CREATE INDEX idx_sessions_agent ON control_plane.sessions (agent_id);
CREATE INDEX idx_sessions_status ON control_plane.sessions (status);

CREATE TABLE control_plane.session_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID REFERENCES task_queue.tasks(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    data            JSONB,
    archived_at     TIMESTAMPTZ
);

CREATE INDEX idx_session_events_task ON control_plane.session_events (task_id, timestamp);
CREATE INDEX idx_session_events_type ON control_plane.session_events (event_type);

CREATE TABLE control_plane.approvals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL REFERENCES task_queue.tasks(id) ON DELETE CASCADE,
    workflow        TEXT,
    approval_kind   TEXT NOT NULL DEFAULT 'operator_approval',
    tool_name       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    request_preview TEXT,
    reason          TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    archived_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_approvals_task ON control_plane.approvals (task_id, requested_at DESC);
CREATE INDEX idx_approvals_status ON control_plane.approvals (status, requested_at DESC);

CREATE TABLE control_plane.schedules (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id    UUID REFERENCES control_plane.agents(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    cron        TEXT NOT NULL,
    prompt      TEXT,
    enabled     BOOLEAN DEFAULT TRUE,
    last_run    TIMESTAMPTZ,
    next_run    TIMESTAMPTZ,
    created     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_schedules_agent ON control_plane.schedules (agent_id);
CREATE INDEX idx_schedules_enabled ON control_plane.schedules (enabled) WHERE enabled = TRUE;

CREATE TABLE control_plane.background_job_runs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_type     TEXT NOT NULL,
    scope        TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    duration_sec FLOAT,
    summary      JSONB NOT NULL DEFAULT '{}',
    warnings     JSONB NOT NULL DEFAULT '[]',
    error        TEXT
);

CREATE INDEX idx_background_job_runs_job_type ON control_plane.background_job_runs (job_type, started_at DESC);
CREATE INDEX idx_background_job_runs_started_at ON control_plane.background_job_runs (started_at DESC);

-- Singleton row (id=1) tracking the connected workflow repo's pinned
-- version and last sync outcome. Bootstrap-owned repo URL/PAT are never
-- stored here.
CREATE TABLE control_plane.workflow_repo_state (
    id                   INT PRIMARY KEY DEFAULT 1,
    pinned_ref           TEXT,
    last_synced_ref      TEXT,
    last_synced_commit   TEXT,
    last_synced_at       TIMESTAMPTZ,
    last_sync_status     TEXT,
    last_sync_error      TEXT,
    discovered_workflows JSONB NOT NULL DEFAULT '[]',
    bundle_errors        JSONB NOT NULL DEFAULT '{}',
    updated              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT workflow_repo_state_singleton CHECK (id = 1)
);

-- ─── Updated-at trigger ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tasks_updated_at
    BEFORE UPDATE ON task_queue.tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER agents_updated_at
    BEFORE UPDATE ON control_plane.agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
