'use client';

import { useEffect, useMemo, useState } from 'react';
import { apiFetch, type BackgroundJobRun, type PlatformBackgroundJobs } from '@/lib/api';

const PAGE_SIZE = 5;
const EMPTY_BACKGROUND: PlatformBackgroundJobs = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 };

export default function HousekeepingPage() {
  const [data, setData] = useState<PlatformBackgroundJobs>(EMPTY_BACKGROUND);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      const payload = await apiFetch<PlatformBackgroundJobs>(`/api/platform/background-jobs?limit=${PAGE_SIZE}&offset=0`);
      setData(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load housekeeping runs');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const latestRun = data.items[0] || null;
  const previousRuns = data.items.slice(1);
  const latestSummaryItems = useMemo(() => {
    if (!latestRun) {
      return [] as Array<[string, number]>;
    }
    return Object.entries(latestRun.summary || {})
      .filter(([, value]) => typeof value === 'number')
      .map(([key, value]) => [key, value as number] as [string, number]);
  }, [latestRun]);

  const stats = useMemo(() => {
    return {
      runs: data.items.length,
      warnings: data.items.reduce((sum, job) => sum + job.warnings.length, 0),
      failures: data.items.filter((job) => job.status !== 'succeeded').length,
      latestStatus: latestRun ? humanizeLabel(latestRun.status) : 'No runs',
    };
  }, [data, latestRun]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">Housekeeping</p>
          <h1 className="mt-2 font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
            Background maintenance runs
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
        <Stat label="Runs" value={String(stats.runs)} tone="neutral" />
        <Stat label="Warnings" value={String(stats.warnings)} tone="warning" />
        <Stat label="Failures" value={String(stats.failures)} tone="error" />
        <Stat label="Latest Status" value={stats.latestStatus} tone={latestRun?.status === 'succeeded' ? 'success' : latestRun ? 'warning' : 'neutral'} />
      </div>

      {latestRun ? (
        <div className="rounded-card border border-ops-border bg-ops-surface p-6 shadow-card">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <p className="text-[11px] uppercase tracking-[0.22em] text-[var(--color-text-tertiary)]">Latest Run</p>

            <StatusBadge status={latestRun.status} />
          </div>

          <div className="mt-5 grid gap-3 md:grid-cols-3">
            <MetaBlock label="Started" value={formatDateTime(latestRun.started_at)} />
            <MetaBlock label="Finished" value={latestRun.finished_at ? formatDateTime(latestRun.finished_at) : 'Running'} />
            <MetaBlock label="Duration" value={formatDurationSeconds(latestRun.duration_sec)} />
          </div>

          {latestSummaryItems.length > 0 ? (
            <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {latestSummaryItems.map(([key, value]) => (
                <SummaryStat key={key} label={humanizeLabel(key)} value={String(value)} />
              ))}
            </div>
          ) : null}

          {latestRun.warnings.length > 0 ? <MessagePanel messages={latestRun.warnings} tone="warning" className="mt-5" /> : null}

          {latestRun.error ? (
            <div className="mt-5 rounded-[16px] border border-[var(--color-error)]/15 bg-[var(--color-error-muted)] px-4 py-3 text-sm leading-6 text-[var(--color-text-secondary)]">
              {latestRun.error}
            </div>
          ) : null}
        </div>
      ) : null}

      {loading ? <p className="text-sm text-[var(--color-text-tertiary)]">Loading housekeeping runs...</p> : null}
      {error ? <p className="text-sm text-[var(--color-error)]">{error}</p> : null}

      {!loading && !error ? (
        <>
          <div className="space-y-3">
            {data.items.length === 0 ? (
              <EmptyMessage title="No background jobs recorded yet" description="Housekeeping runs will appear here once the session manager persists them." />
            ) : previousRuns.length > 0 ? (
              previousRuns.map((job) => <BackgroundJobRow key={job.id} job={job} />)
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}

function BackgroundJobRow({ job }: { job: BackgroundJobRun }) {
  const summaryItems = Object.entries(job.summary || {}).filter(([, value]) => typeof value === 'number');

  return (
    <div className="rounded-[20px] border border-ops-border bg-[var(--color-surface-raised)] p-5">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div className="space-y-3">
          <StatusBadge status={job.status} />

          {job.error ? (
            <div className="rounded-[16px] border border-[var(--color-error)]/15 bg-[var(--color-error-muted)] px-4 py-3 text-sm leading-6 text-[var(--color-text-secondary)]">
              {job.error}
            </div>
          ) : null}

          {job.warnings.length > 0 ? <MessagePanel messages={job.warnings} tone="warning" /> : null}

          {summaryItems.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {summaryItems.map(([key, value]) => (
                <SmallBadge key={key} label={`${humanizeLabel(key)}: ${String(value)}`} tone="neutral" />
              ))}
            </div>
          ) : null}
        </div>

        <div className="grid min-w-[220px] grid-cols-2 gap-3 text-sm xl:max-w-[320px]">
          <MetaBlock label="Started" value={formatDateTime(job.started_at)} />
          <MetaBlock label="Finished" value={job.finished_at ? formatDateTime(job.finished_at) : 'Running'} />
          <MetaBlock label="Duration" value={formatDurationSeconds(job.duration_sec)} />
          <MetaBlock label="Warnings" value={String(job.warnings.length)} />
        </div>
      </div>
    </div>
  );
}

function MessagePanel({
  messages,
  tone,
  className = '',
}: {
  messages: string[];
  tone: 'warning';
  className?: string;
}) {
  const toneClasses = {
    warning: 'border-[var(--color-warning)]/15 bg-[var(--color-warning-muted)]',
  };

  return (
    <div className={`rounded-[16px] border px-4 py-3 text-sm leading-6 text-[var(--color-text-secondary)] ${toneClasses[tone]} ${className}`.trim()}>
      <ul className="space-y-1">
        {messages.map((message, index) => (
          <li key={`${message}-${index}`}>{message}</li>
        ))}
      </ul>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'neutral' | 'warning' | 'success' | 'error';
}) {
  const tones = {
    neutral: 'bg-ops-surface text-[var(--color-text-primary)] border-ops-border',
    warning: 'bg-[var(--color-warning-muted)] text-[var(--color-warning)] border-[var(--color-warning)]/25',
    success: 'bg-[var(--color-success-muted)] text-[var(--color-success)] border-[var(--color-success)]/25',
    error: 'bg-[var(--color-error-muted)] text-[var(--color-error)] border-[var(--color-error)]/25',
  };

  return (
    <div className={`rounded-[14px] border px-4 py-3 ${tones[tone]}`}>
      <div className="text-[10px] uppercase tracking-[0.16em]">{label}</div>
      <div className="mt-2 text-xl font-medium">{value}</div>
    </div>
  );
}

function SummaryStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[16px] border border-ops-border bg-[var(--color-surface-raised)] px-4 py-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-2 text-lg font-medium text-[var(--color-text-primary)]">{value}</div>
    </div>
  );
}

function MetaBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[16px] border border-ops-border bg-ops-surface px-4 py-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-2 text-sm font-medium text-[var(--color-text-primary)]">{value}</div>
    </div>
  );
}

function EmptyMessage({ title, description }: { title: string; description: string }) {
  return (
    <div className="rounded-[18px] border border-dashed border-ops-border bg-[var(--color-surface-raised)] px-5 py-8 text-center">
      <div className="text-sm font-medium text-[var(--color-text-primary)]">{title}</div>
      <div className="mt-2 text-sm leading-6 text-[var(--color-text-secondary)]">{description}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone = ['approved', 'succeeded'].includes(status) ? 'success' : ['rejected', 'failed'].includes(status) ? 'error' : 'warning';
  const toneClasses = {
    success: 'bg-[var(--color-success-muted)] text-[var(--color-success)]',
    warning: 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]',
    error: 'bg-[var(--color-error-muted)] text-[var(--color-error)]',
  };

  return (
    <span className={`rounded-full px-3 py-1 text-[11px] uppercase tracking-[0.14em] ${toneClasses[tone]}`}>
      {status}
    </span>
  );
}

function SmallBadge({
  label,
  tone,
}: {
  label: string;
  tone: 'neutral' | 'info';
}) {
  const toneClasses = {
    neutral: 'bg-ops-surface text-[var(--color-text-tertiary)]',
    info: 'bg-[var(--color-info-muted)] text-[var(--color-info)]',
  };

  return <span className={`rounded-full px-3 py-1 text-[11px] uppercase tracking-[0.14em] ${toneClasses[tone]}`}>{label}</span>;
}

function formatDateTime(value: string) {
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatDurationSeconds(value: number | null) {
  if (value === null) return '-';
  if (value > 0 && value < 1) return '<1s';
  if (value < 60) return `${Math.round(value)}s`;
  return `${(value / 60).toFixed(1)}m`;
}

function humanizeLabel(value: string) {
  return value.replace(/[-_]/g, ' ');
}