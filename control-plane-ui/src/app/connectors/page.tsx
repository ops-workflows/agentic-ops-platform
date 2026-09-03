'use client';

import { useCallback, useEffect, useState } from 'react';
import { Pause, Play, RefreshCw } from 'lucide-react';
import { apiFetch, type Connector } from '@/lib/api';

function humanize(value: string) {
  return value.replace(/[_-]/g, ' ');
}

export default function ConnectorsPage() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const payload = await apiFetch<Connector[]>('/api/platform/connectors');
      setConnectors(payload);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Failed to load connectors',
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function toggleConnector(connector: Connector) {
    setTogglingId(connector.id);
    setError(null);
    try {
      const action = connector.paused ? 'resume' : 'pause';
      const updated = await apiFetch<Connector>(
        `/api/platform/connectors/${connector.id}/${action}`,
        { method: 'POST' },
      );
      setConnectors((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Failed to update connector',
      );
    } finally {
      setTogglingId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
            Connectors
          </h1>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
        >
          <RefreshCw aria-hidden="true" className="h-4 w-4" />
          Refresh
        </button>
      </div>

      {loading ? (
        <p className="text-sm text-[var(--color-text-tertiary)]">
          Loading connectors...
        </p>
      ) : null}
      {error ? (
        <p className="text-sm text-[var(--color-error)]">{error}</p>
      ) : null}

      {!loading && !error ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {connectors.map((connector) => (
            <article
              key={connector.id}
              className="rounded-card border border-ops-border bg-ops-surface p-5"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
                    {connector.name}
                  </h2>
                  <p className="mt-1 text-sm leading-6 text-[var(--color-text-secondary)]">
                    {connector.summary}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <span className="rounded-full bg-ops-surface-raised px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
                    {humanize(connector.source_type)}
                  </span>
                  <span
                    className={`rounded-full px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] ${
                      connector.paused
                        ? 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]'
                        : 'bg-[var(--color-success-muted)] text-[var(--color-success)]'
                    }`}
                  >
                    {connector.paused ? 'Paused' : 'Active'}
                  </span>
                </div>
              </div>

              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <Meta label="Source" value={connector.source_label} />
                <Meta
                  label="Target workflow"
                  value={connector.target_workflow || 'Unassigned'}
                />
                <Meta
                  label="Target channel"
                  value={connector.target_channel || '-'}
                />
                <Meta label="Type" value={humanize(connector.type)} />
              </div>

              {connector.tags.length > 0 ? (
                <div className="mt-4 flex flex-wrap gap-2">
                  {connector.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full bg-[var(--color-info-muted)] px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-info)]"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}

              <div className="mt-5 border-t border-ops-border-subtle pt-4">
                <button
                  type="button"
                  onClick={() => toggleConnector(connector)}
                  disabled={togglingId === connector.id}
                  className="inline-flex items-center gap-2 rounded-btn border border-ops-border bg-ops-surface-raised px-3 py-2 text-sm text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)]/40 hover:text-[var(--color-text-primary)] disabled:cursor-wait disabled:opacity-50"
                >
                  {connector.paused ? (
                    <Play aria-hidden="true" className="h-4 w-4" />
                  ) : (
                    <Pause aria-hidden="true" className="h-4 w-4" />
                  )}
                  {connector.paused ? 'Resume' : 'Pause'}
                </button>
              </div>
            </article>
          ))}
          {connectors.length === 0 ? (
            <p className="text-sm text-[var(--color-text-tertiary)]">
              No connectors found.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[12px] border border-ops-border-subtle bg-[var(--color-surface-raised)] px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className="mt-1 text-sm text-[var(--color-text-secondary)]">
        {value}
      </div>
    </div>
  );
}
