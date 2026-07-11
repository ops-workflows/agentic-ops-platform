'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type WorkflowRepoStatus, type WorkflowRepoVersion } from '@/lib/api';

export default function WorkflowRepoPage() {
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
      setSelectedRef(statusPayload.pinned_ref || statusPayload.default_ref || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workflow repo status');
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
      const payload = await apiFetch<WorkflowRepoStatus>('/api/platform/workflow-repo/sync', { method: 'POST' });
      setStatus(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sync failed');
    } finally {
      setSyncing(false);
    }
  }

  async function handlePin() {
    if (!selectedRef) return;
    setPinning(true);
    setError(null);
    try {
      const payload = await apiFetch<WorkflowRepoStatus>('/api/platform/workflow-repo/pin', {
        method: 'POST',
        body: JSON.stringify({ ref: selectedRef }),
      });
      setStatus(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pin failed');
    } finally {
      setPinning(false);
    }
  }

  const bundleErrorEntries = status ? Object.entries(status.bundle_errors) : [];

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">Workflow Repo</p>
          <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
            Sync &amp; versioning
          </h1>
        </div>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)] disabled:opacity-50"
        >
          {syncing ? 'Syncing...' : 'Sync now'}
        </button>
      </div>

      {loading ? <p className="text-sm text-[var(--color-text-tertiary)]">Loading workflow repo status...</p> : null}
      {error ? <p className="text-sm text-[var(--color-error)]">{error}</p> : null}

      {!loading && status ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <article className="rounded-card border border-ops-border bg-ops-surface p-5">
            <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Source</h2>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <Meta label="Mode" value={status.source_mode} />
              <Meta label="URL" value={status.source_url || 'Local checkout'} />
              <Meta label="Default ref" value={status.default_ref || '-'} />
              <Meta label="Pinned ref" value={status.pinned_ref || 'Not pinned'} />
            </div>

            {status.source_mode === 'remote' ? (
              <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center">
                <select
                  value={selectedRef}
                  onChange={(e) => setSelectedRef(e.target.value)}
                  className="rounded-btn border border-ops-border bg-ops-surface-raised px-3 py-2 text-sm text-[var(--color-text-primary)]"
                >
                  {status.default_ref ? <option value={status.default_ref}>{status.default_ref} (default)</option> : null}
                  {versions.map((version) => (
                    <option key={version.name} value={version.name}>
                      {version.name}
                    </option>
                  ))}
                </select>
                <button
                  onClick={handlePin}
                  disabled={pinning || !selectedRef}
                  className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)] disabled:opacity-50"
                >
                  {pinning ? 'Pinning...' : 'Pin version'}
                </button>
              </div>
            ) : (
              <p className="mt-4 text-sm text-[var(--color-text-tertiary)]">
                No remote workflow repo configured — running from a local checkout.
              </p>
            )}
          </article>

          <article className="rounded-card border border-ops-border bg-ops-surface p-5">
            <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Last sync</h2>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <Meta label="Status" value={status.last_sync_status || 'Never synced'} />
              <Meta label="Synced ref" value={status.last_synced_ref || '-'} />
              <Meta label="Commit" value={status.last_synced_commit ? status.last_synced_commit.slice(0, 12) : '-'} />
              <Meta label="Synced at" value={status.last_synced_at || '-'} />
            </div>
            {status.last_sync_error ? (
              <p className="mt-4 text-sm text-[var(--color-error)]">{status.last_sync_error}</p>
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
                  <span className="text-sm text-[var(--color-text-tertiary)]">None discovered yet.</span>
                ) : null}
              </div>
            </div>

            {bundleErrorEntries.length > 0 ? (
              <div className="mt-4">
                <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-error)]">Bundle errors</div>
                <ul className="mt-2 space-y-1">
                  {bundleErrorEntries.map(([workflow, message]) => (
                    <li key={workflow} className="text-sm text-[var(--color-error)]">
                      <span className="font-medium">{workflow}</span>: {message}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </article>
        </div>
      ) : null}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[12px] border border-ops-border-subtle bg-[var(--color-surface-raised)] px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-1 text-sm text-[var(--color-text-secondary)]">{value}</div>
    </div>
  );
}
