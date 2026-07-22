'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  type Agent,
  apiFetch,
  type WorkflowRepoStatus,
  type WorkflowRepoVersion,
} from '@/lib/api';
import {
  getAgentModelBadgeClasses,
  getAgentModelInfo,
} from '@/lib/agent-model';

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
          <StatCard label="Workflows" value={agents.length} />
          <StatCard label="Active" value={activeCount} />
          <StatCard label="Schedules" value={totalSchedules} />
        </div>
      </section>

      <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
        Workflows
      </h2>

      {loading ? (
        <p className="text-[var(--color-text-tertiary)]">
          Loading workflows...
        </p>
      ) : agents.length === 0 ? (
        <p className="text-[var(--color-text-tertiary)]">No workflows found.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {agents.map((agent) => {
            const modelInfo = getAgentModelInfo(agent.config);

            return (
              <div
                key={agent.id}
                className="group rounded-card border border-ops-border bg-ops-surface p-6 no-underline transition-all duration-200 hover:border-[var(--color-accent)]/30 hover:shadow-card-hover"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="space-y-3 min-w-0 flex-1">
                    <div className="flex items-center gap-3">
                      <AgentIcon name={agent.name} />
                      <div className="min-w-0">
                        <div className="text-base font-medium text-[var(--color-text-primary)]">
                          {agent.name}
                        </div>
                        <div className="text-[10px] text-[var(--color-text-tertiary)]">
                          v{agent.version || '0.0'}
                        </div>
                      </div>
                    </div>
                    <p className="text-sm leading-6 text-[var(--color-text-secondary)]">
                      {agent.description || 'No description provided.'}
                    </p>
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-2">
                    {modelInfo && (
                      <span
                        className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${getAgentModelBadgeClasses(modelInfo.tone)}`}
                      >
                        {modelInfo.label}
                      </span>
                    )}
                    <span
                      className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${agent.paused ? 'border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)]' : 'border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)]'}`}
                    >
                      {agent.paused ? 'Paused' : 'Active'}
                    </span>
                  </div>
                </div>

                <div className="mt-5 grid gap-2 sm:grid-cols-3">
                  <MetaPill label="Path" value={agent.repo_path || 'n/a'} />
                  <MetaPill
                    label="Updated"
                    value={new Date(agent.updated).toLocaleDateString()}
                  />
                  <MetaPill
                    label="Schedules"
                    value={String(
                      (
                        ((agent.config as Record<string, unknown>).schedules as
                          | Array<unknown>
                          | undefined) || []
                      ).length,
                    )}
                  />
                </div>

                <div className="mt-5 flex items-center justify-between">
                  <button
                    type="button"
                    onClick={() => toggleAgentPaused(agent)}
                    disabled={togglingAgent === agent.id}
                    className={`rounded-btn px-3 py-1.5 text-xs font-medium transition-all ${agent.paused ? 'border border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)] hover:bg-[var(--color-success)]/20' : 'border border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)] hover:bg-[var(--color-warning)]/20'} disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    {togglingAgent === agent.id
                      ? agent.paused
                        ? 'Resuming…'
                        : 'Pausing…'
                      : agent.paused
                        ? 'Resume'
                        : 'Pause'}
                  </button>
                  <a
                    href={`/workflows/${agent.name}`}
                    className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
                  >
                    Open workflow
                  </a>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <WorkflowRepoSyncSection />
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

  return (
    <section className="border-t border-ops-border pt-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
            Workflow Sync
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
        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
          <article className="rounded-card border border-ops-border bg-ops-surface p-5">
            <h3 className="text-base font-medium text-[var(--color-text-primary)]">
              Source
            </h3>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <WorkflowRepoMeta label="Mode" value={status.source_mode} />
              <WorkflowRepoMeta
                label="URL"
                value={status.source_url || 'Local checkout'}
              />
              <WorkflowRepoMeta
                label="Default ref"
                value={status.default_ref || '-'}
              />
              <WorkflowRepoMeta
                label="Pinned ref"
                value={status.pinned_ref || 'Not pinned'}
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
            ) : (
              <p className="mt-4 text-sm text-[var(--color-text-tertiary)]">
                Running from a local workflow checkout.
              </p>
            )}
          </article>

          <article className="rounded-card border border-ops-border bg-ops-surface p-5">
            <h3 className="text-base font-medium text-[var(--color-text-primary)]">
              Last Sync
            </h3>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <WorkflowRepoMeta
                label="Status"
                value={status.last_sync_status || 'Never synced'}
              />
              <WorkflowRepoMeta
                label="Synced ref"
                value={status.last_synced_ref || '-'}
              />
              <WorkflowRepoMeta
                label="Commit"
                value={
                  status.last_synced_commit
                    ? status.last_synced_commit.slice(0, 12)
                    : '-'
                }
              />
              <WorkflowRepoMeta
                label="Synced at"
                value={status.last_synced_at || '-'}
              />
            </div>
            {status.last_sync_error ? (
              <p className="mt-4 text-sm text-[var(--color-error)]">
                {status.last_sync_error}
              </p>
            ) : null}
            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
                Discovered workflows
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                {status.discovered_workflows.map((workflow) => (
                  <span
                    key={workflow}
                    className="rounded-full bg-[var(--color-info-muted)] px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-info)]"
                  >
                    {workflow}
                  </span>
                ))}
                {status.discovered_workflows.length === 0 ? (
                  <span className="text-sm text-[var(--color-text-tertiary)]">
                    None discovered yet.
                  </span>
                ) : null}
              </div>
            </div>
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
        </div>
      ) : null}
    </section>
  );
}

function WorkflowRepoMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-btn border border-ops-border-subtle bg-ops-bg px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className="mt-1 break-words text-sm text-[var(--color-text-secondary)]">
        {value}
      </div>
    </div>
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

function MetaPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[10px] border border-ops-border-subtle bg-ops-bg px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div
        className="mt-1 truncate text-sm text-[var(--color-text-secondary)]"
        title={value}
      >
        {value}
      </div>
    </div>
  );
}

function AgentIcon({ name }: { name: string }) {
  /* Generate a deterministic hue from the agent name */
  let hash = 0;
  for (let i = 0; i < name.length; i++)
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  const hue = Math.abs(hash) % 360;
  const color = `hsl(${hue}, 45%, 55%)`;

  return (
    <svg
      width="36"
      height="36"
      viewBox="0 0 36 36"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="flex-none"
    >
      <rect
        width="36"
        height="36"
        rx="10"
        fill="var(--color-surface-raised)"
        stroke="var(--color-border)"
        strokeWidth="1"
      />
      <circle
        cx="18"
        cy="14"
        r="4"
        stroke={color}
        strokeWidth="1.8"
        fill="none"
      />
      <path
        d="M11 26c0-3.87 3.13-7 7-7s7 3.13 7 7"
        stroke={color}
        strokeWidth="1.8"
        strokeLinecap="round"
        fill="none"
      />
      <circle cx="24" cy="12" r="2" fill={color} opacity="0.4" />
    </svg>
  );
}
