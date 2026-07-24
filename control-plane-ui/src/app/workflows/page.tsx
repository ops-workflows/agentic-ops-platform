'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  type Agent,
  apiFetch,
  type WorkflowRepoStatus,
  type WorkflowRepoVersion,
} from '@/lib/api';
import { WorkflowsListSection } from '@/components/workflows-list-section';
import { ExpandableMeta } from '@/components/expandable-meta';
import { formatWorkflowDate } from '@/lib/workflow-format';

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [togglingAgent, setTogglingAgent] = useState<string | null>(null);

  useEffect(() => {
    loadAgents();
  }, []);

  async function loadAgents() {
    setLoading(true);
    try {
      const data = await apiFetch<Agent[]>('/api/agents');
      setAgents(data);
    } catch (e) {
      console.error('Failed to load agents:', e);
    }
    setLoading(false);
  }

  async function toggleAgentPaused(agent: Agent) {
    setTogglingAgent(agent.id);
    try {
      const updated = await apiFetch<Agent>(
        `/api/agents/${agent.name}/${agent.paused ? 'resume' : 'pause'}`,
        {
          method: 'POST',
        },
      );
      setAgents((current) =>
        current.map((entry) => (entry.id === updated.id ? updated : entry)),
      );
    } catch (e) {
      console.error(`Failed to ${agent.paused ? 'resume' : 'pause'} agent:`, e);
    } finally {
      setTogglingAgent(null);
    }
  }

  const totalSchedules = agents.reduce((sum, agent) => {
    const config = agent.config as Record<string, unknown>;
    return (
      sum + ((config.schedules as Array<unknown> | undefined) || []).length
    );
  }, 0);

  const activeCount = agents.filter((a) => !a.paused).length;

  return (
    <div className="space-y-10">
      <section className="space-y-4 pt-4">
        <div className="flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="max-w-2xl space-y-4">
            <h1 className="font-display text-4xl font-normal leading-[1.15] tracking-tight text-[var(--color-text-primary)]">
              Workflows
            </h1>
          </div>
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <StatCard label="Total" value={agents.length} />
          <StatCard label="Active" value={activeCount} />
          <StatCard label="Schedules" value={totalSchedules} />
        </div>
      </section>

      <WorkflowRepoSyncSection />

      <WorkflowsListSection
        agents={agents}
        loading={loading}
        togglingAgent={togglingAgent}
        onToggleAgentPaused={toggleAgentPaused}
      />
    </div>
  );
}

function WorkflowRepoSyncSection() {
  const [status, setStatus] = useState<WorkflowRepoStatus | null>(null);
  const [versions, setVersions] = useState<WorkflowRepoVersion[]>([]);
  const [selectedRef, setSelectedRef] = useState('');
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [pinning, setPinning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [statusPayload, versionsPayload] = await Promise.all([
        apiFetch<WorkflowRepoStatus>('/api/platform/workflow-repo'),
        apiFetch<WorkflowRepoVersion[]>('/api/platform/workflow-repo/versions'),
      ]);
      setStatus(statusPayload);
      setVersions(versionsPayload);
      setSelectedRef(
        statusPayload.pinned_ref || statusPayload.default_ref || '',
      );
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : 'Failed to load workflow repo status',
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSync() {
    setSyncing(true);
    setError(null);
    try {
      setStatus(
        await apiFetch<WorkflowRepoStatus>('/api/platform/workflow-repo/sync', {
          method: 'POST',
        }),
      );
    } catch (syncError) {
      setError(syncError instanceof Error ? syncError.message : 'Sync failed');
    } finally {
      setSyncing(false);
    }
  }

  async function handlePin() {
    if (!selectedRef) return;
    setPinning(true);
    setError(null);
    try {
      setStatus(
        await apiFetch<WorkflowRepoStatus>('/api/platform/workflow-repo/pin', {
          method: 'POST',
          body: JSON.stringify({ ref: selectedRef }),
        }),
      );
    } catch (pinError) {
      setError(pinError instanceof Error ? pinError.message : 'Pin failed');
    } finally {
      setPinning(false);
    }
  }

  const bundleErrorEntries = status ? Object.entries(status.bundle_errors) : [];
  const activeRef = status
    ? status.last_synced_ref || status.pinned_ref || status.default_ref || '-'
    : '-';

  return (
    <section className="border-t border-ops-border pt-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
            Sync
          </h2>
          <p className="mt-1 text-sm text-[var(--color-text-tertiary)]">
            Activate workflow bundles and repo-owned task settings for new
            tasks.
          </p>
        </div>
        <button
          type="button"
          onClick={handleSync}
          disabled={syncing || loading}
          className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)] disabled:opacity-50"
        >
          {syncing ? 'Syncing...' : 'Sync now'}
        </button>
      </div>

      {loading ? (
        <p className="mt-4 text-sm text-[var(--color-text-tertiary)]">
          Loading workflow sync status...
        </p>
      ) : null}
      {error ? (
        <p className="mt-4 text-sm text-[var(--color-error)]">{error}</p>
      ) : null}

      {!loading && status ? (
        <article className="mt-4 rounded-card border border-ops-border bg-ops-surface p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="text-base font-medium text-[var(--color-text-primary)]">
              Workflow Repository
            </h3>
            <SyncStatusBadge status={status.last_sync_status} />
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <ExpandableMeta
              label={
                status.source_mode === 'remote' ? 'Source' : 'Local source'
              }
              value={
                status.source_mode === 'remote'
                  ? status.source_url || 'Not configured'
                  : status.source_path || 'Local checkout'
              }
            />
            <ExpandableMeta label="Active ref" value={activeRef} />
            <ExpandableMeta
              label="Commit"
              value={status.last_synced_commit || '-'}
            />
            <ExpandableMeta
              label="Synced at"
              value={formatWorkflowEventDate(status.last_synced_at)}
            />
          </div>
          {status.source_mode === 'remote' ? (
            <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center">
              <select
                value={selectedRef}
                onChange={(event) => setSelectedRef(event.target.value)}
                className="rounded-btn border border-ops-border bg-ops-surface-raised px-3 py-2 text-sm text-[var(--color-text-primary)]"
              >
                {status.default_ref ? (
                  <option value={status.default_ref}>
                    {status.default_ref} (default)
                  </option>
                ) : null}
                {versions.map((version) => (
                  <option key={version.name} value={version.name}>
                    {version.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={handlePin}
                disabled={pinning || !selectedRef}
                className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)] disabled:opacity-50"
              >
                {pinning ? 'Pinning...' : 'Pin version'}
              </button>
            </div>
          ) : null}
          {status.last_sync_error ? (
            <p className="mt-4 text-sm text-[var(--color-error)]">
              {status.last_sync_error}
            </p>
          ) : null}
          {bundleErrorEntries.length > 0 ? (
            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-error)]">
                Bundle errors
              </div>
              <ul className="mt-2 space-y-1">
                {bundleErrorEntries.map(([workflow, message]) => (
                  <li
                    key={workflow}
                    className="text-sm text-[var(--color-error)]"
                  >
                    <span className="font-medium">{workflow}</span>: {message}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </article>
      ) : null}
    </section>
  );
}

function formatWorkflowEventDate(value: string | null): string {
  if (!value) return 'Never synced';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${formatWorkflowDate(value)} · ${date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })}`;
}

function SyncStatusBadge({ status }: { status: string | null }) {
  const tone =
    status === 'ok'
      ? 'text-[var(--color-success)]'
      : status?.includes('error')
        ? 'text-[var(--color-error)]'
        : 'text-[var(--color-text-tertiary)]';

  return (
    <span
      className={`border border-current/40 px-2 py-1 text-[10px] font-bold uppercase tracking-[0.12em] ${tone}`}
    >
      {status || 'Never synced'}
    </span>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-card border border-ops-border bg-ops-surface px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className="mt-2 text-2xl font-medium text-[var(--color-text-primary)]">
        {value}
      </div>
    </div>
  );
}
