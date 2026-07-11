'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiFetch, type PlatformApprovals } from '@/lib/api';

const PAGE_SIZE = 100;

function formatDateTime(value: string | null) {
  if (!value) return '-';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function humanize(value: string) {
  return value.replace(/[_-]/g, ' ');
}

export default function ApprovalsPage() {
  const [data, setData] = useState<PlatformApprovals>({ counts_by_status: {}, items: [], total: 0, limit: PAGE_SIZE, offset: 0 });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const payload = await apiFetch<PlatformApprovals>(`/api/platform/approvals?limit=${PAGE_SIZE}&offset=${offset}`);
      setData(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load approvals');
    } finally {
      setLoading(false);
    }
  }, [offset]);

  useEffect(() => {
    load();
  }, [load]);

  const counts = useMemo(() => {
    return {
      pending: data.counts_by_status.pending || 0,
      approved: data.counts_by_status.approved || 0,
      rejected: data.counts_by_status.rejected || 0,
      total: data.total,
    };
  }, [data]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">Approvals</p>
          <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
            Approval inbox
          </h1>
        </div>
        <button
          onClick={load}
          className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
        >
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Total" value={counts.total} tone="neutral" />
        <Stat label="Pending" value={counts.pending} tone="warning" />
        <Stat label="Approved" value={counts.approved} tone="success" />
        <Stat label="Rejected" value={counts.rejected} tone="error" />
      </div>

      {loading ? <p className="text-sm text-[var(--color-text-tertiary)]">Loading approvals...</p> : null}
      {error ? <p className="text-sm text-[var(--color-error)]">{error}</p> : null}

      {!loading && !error ? (
        <>
          <div className="overflow-x-auto rounded-card border border-ops-border bg-ops-surface">
            <table className="w-full min-w-[980px]">
            <thead>
              <tr className="border-b border-ops-border text-left text-xs uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Workflow</th>
                <th className="px-4 py-3 font-medium">Tool</th>
                <th className="px-4 py-3 font-medium">Input Preview</th>
                <th className="px-4 py-3 font-medium">Requested</th>
                <th className="px-4 py-3 font-medium">Resolved</th>
                <th className="px-4 py-3 font-medium">Resolved By</th>
                <th className="px-4 py-3 font-medium">Task</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id} className="border-b border-ops-border/50 text-sm align-top last:border-0">
                  <td className="px-4 py-3">
                    <StatusBadge status={item.status} />
                  </td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">{item.workflow || '-'}</td>
                  <td className="px-4 py-3">
                    <div className="font-medium text-[var(--color-text-primary)]">{humanize(item.tool_name)}</div>
                    <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">{item.approval_kind}</div>
                  </td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">
                    <div className="max-w-[320px] truncate">{item.request_preview || '-'}</div>
                  </td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">{formatDateTime(item.requested_at)}</td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">{formatDateTime(item.resolved_at)}</td>
                  <td className="px-4 py-3 text-[var(--color-text-secondary)]">
                    {item.resolved_by || item.resolved_by_user_id || '-'}
                  </td>
                  <td className="px-4 py-3">
                    <a href={`/tasks/${item.task_id}`} className="text-[var(--color-accent)] hover:text-[var(--color-accent-hover)]">
                      {item.task_id.slice(0, 8)}
                    </a>
                  </td>
                </tr>
              ))}
              {data.items.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-10 text-center text-sm text-[var(--color-text-tertiary)]">
                    No approvals recorded yet
                  </td>
                </tr>
              ) : null}
            </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between text-sm text-[var(--color-text-tertiary)]">
            <span>{data.total === 0 ? '0 approvals' : `${offset + 1}-${Math.min(offset + PAGE_SIZE, data.total)} of ${data.total}`}</span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                className="rounded-btn border border-ops-border bg-ops-surface px-3 py-1.5 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={offset + PAGE_SIZE >= data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
                className="rounded-btn border border-ops-border bg-ops-surface px-3 py-1.5 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: 'neutral' | 'warning' | 'success' | 'error' }) {
  const tones = {
    neutral: 'bg-ops-surface text-[var(--color-text-primary)] border-ops-border',
    warning: 'bg-[var(--color-warning-muted)] text-[var(--color-warning)] border-[var(--color-warning)]/25',
    success: 'bg-[var(--color-success-muted)] text-[var(--color-success)] border-[var(--color-success)]/25',
    error: 'bg-[var(--color-error-muted)] text-[var(--color-error)] border-[var(--color-error)]/25',
  };
  return (
    <div className={`rounded-[14px] border px-4 py-3 ${tones[tone]}`}>
      <div className="text-[10px] uppercase tracking-[0.16em]">{label}</div>
      <div className="mt-2 text-2xl font-medium">{value}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tones: Record<string, string> = {
    approved: 'bg-[var(--color-success-muted)] text-[var(--color-success)]',
    rejected: 'bg-[var(--color-error-muted)] text-[var(--color-error)]',
    pending: 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]',
  };

  return (
    <span className={`rounded-full px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] ${tones[status] || 'bg-ops-surface-raised text-[var(--color-text-tertiary)]'}`}>
      {status}
    </span>
  );
}
