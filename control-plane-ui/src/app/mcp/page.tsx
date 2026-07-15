'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { apiFetch, type McpServer } from '@/lib/api';

function humanize(value: string) {
  return value.replace(/_/g, ' ').replace(/mcp__/g, '').replace(/__/g, ' / ');
}

export default function McpPage() {
  return (
    <Suspense fallback={<McpPageFallback />}>
      <McpPageContent />
    </Suspense>
  );
}

function McpPageContent() {
  const searchParams = useSearchParams();
  const requestedServerId = searchParams?.get('server') || '';
  const [servers, setServers] = useState<McpServer[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<McpServer[]>('/api/platform/mcp')
      .then((payload) => {
        setServers(payload);
        setSelectedId(payload[0]?.id || '');
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Failed to load MCP'),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!servers.length) {
      return;
    }

    if (requestedServerId) {
      const match = servers.find((server) => server.id === requestedServerId);
      if (match) {
        setSelectedId(requestedServerId);
      }
    }
  }, [requestedServerId, servers]);

  useEffect(() => {
    if (!servers.length) {
      return;
    }

    if (!selectedId || !servers.some((server) => server.id === selectedId)) {
      setSelectedId(servers[0]?.id || '');
    }
  }, [selectedId, servers]);

  const selected = useMemo(
    () => servers.find((item) => item.id === selectedId) || null,
    [servers, selectedId],
  );

  return (
    <div className="space-y-6">
      <div>
        <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">
          MCP
        </p>
        <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
          Tool catalog
        </h1>
      </div>

      {loading ? (
        <p className="text-sm text-[var(--color-text-tertiary)]">
          Loading MCP catalog...
        </p>
      ) : null}
      {error ? (
        <p className="text-sm text-[var(--color-error)]">{error}</p>
      ) : null}

      {!loading && !error ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_1fr]">
          <aside className="max-h-[70vh] overflow-y-auto rounded-card border border-ops-border bg-ops-surface">
            {servers.map((server) => {
              const active = selectedId === server.id;
              return (
                <button
                  key={server.id}
                  onClick={() => setSelectedId(server.id)}
                  className={`w-full border-b border-ops-border px-4 py-3 text-left transition-colors last:border-0 ${
                    active
                      ? 'bg-[var(--color-accent-muted)]'
                      : 'hover:bg-[var(--color-surface-raised)]'
                  }`}
                >
                  <div className="text-sm font-medium text-[var(--color-text-primary)]">
                    {server.name}
                  </div>
                  <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                    {server.tools.length} tools
                  </div>
                </button>
              );
            })}
          </aside>

          <section className="rounded-card border border-ops-border bg-ops-surface p-5">
            {selected ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-2xl font-medium text-[var(--color-text-primary)]">
                    {selected.name}
                  </h2>
                  <Badge
                    label={`${selected.usage_count} workflows`}
                    tone="neutral"
                  />
                </div>
                <p className="mt-2 text-sm leading-6 text-[var(--color-text-secondary)]">
                  {selected.description}
                </p>

                <div className="mt-4 flex flex-wrap gap-2">
                  {selected.used_by.length > 0 ? (
                    selected.used_by.map((workflow) => (
                      <Badge key={workflow} label={workflow} tone="info" />
                    ))
                  ) : (
                    <Badge label="Not bound to workflows" tone="warning" />
                  )}
                </div>

                <div className="mt-6 divide-y divide-ops-border rounded-[14px] border border-ops-border">
                  {selected.tools.map((tool) => (
                    <div key={tool.name} className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="font-medium text-[var(--color-text-primary)]">
                          {humanize(tool.name)}
                        </div>
                        <Badge
                          label={tool.read_only ? 'Read only' : 'Mutable'}
                          tone={tool.read_only ? 'success' : 'warning'}
                        />
                        {tool.open_world ? (
                          <Badge label="External" tone="neutral" />
                        ) : null}
                      </div>
                      <div className="mt-1 text-sm text-[var(--color-text-secondary)]">
                        {tool.description}
                      </div>
                    </div>
                  ))}
                  {selected.tools.length === 0 ? (
                    <div className="px-4 py-6 text-sm text-[var(--color-text-tertiary)]">
                      No tools discovered.
                    </div>
                  ) : null}
                </div>
              </>
            ) : (
              <p className="text-sm text-[var(--color-text-tertiary)]">
                Select an MCP server.
              </p>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}

function McpPageFallback() {
  return (
    <div className="space-y-6">
      <div>
        <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">
          MCP
        </p>
        <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
          Tool catalog
        </h1>
      </div>
      <p className="text-sm text-[var(--color-text-tertiary)]">
        Loading MCP catalog...
      </p>
    </div>
  );
}

function Badge({
  label,
  tone,
}: {
  label: string;
  tone: 'neutral' | 'success' | 'warning' | 'info';
}) {
  const classes = {
    neutral: 'bg-ops-surface-raised text-[var(--color-text-tertiary)]',
    success: 'bg-[var(--color-success-muted)] text-[var(--color-success)]',
    warning: 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]',
    info: 'bg-[var(--color-info-muted)] text-[var(--color-info)]',
  };

  return (
    <span
      className={`rounded-full px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] ${classes[tone]}`}
    >
      {label}
    </span>
  );
}
