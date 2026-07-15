'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type Connector } from '@/lib/api';

function humanize(value: string) {
  return value.replace(/[_-]/g, ' ');
}

export default function ConnectorsPage() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">
            Connectors
          </p>
          <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
            Ingestion surfaces
          </h1>
        </div>
        <button
          onClick={load}
          className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
        >
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
                <span className="rounded-full bg-ops-surface-raised px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
                  {humanize(connector.source_type)}
                </span>
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
