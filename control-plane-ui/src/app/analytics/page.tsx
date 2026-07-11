'use client';

import { useEffect, useState, useCallback } from 'react';
import { apiFetch, Analytics } from '@/lib/api';

function monthLabel(date: Date) {
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
}

function monthRange(date: Date) {
  const start = new Date(date.getFullYear(), date.getMonth(), 1);
  const end = new Date(date.getFullYear(), date.getMonth() + 1, 0);
  return { start, end };
}

function daysBetween(a: Date, b: Date) {
  return Math.ceil((b.getTime() - a.getTime()) / (1000 * 60 * 60 * 24)) + 1;
}

export default function AnalyticsPage() {
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [currentMonth, setCurrentMonth] = useState(() => new Date());

  const fetchForMonth = useCallback((month: Date) => {
    setLoading(true);
    const { start, end } = monthRange(month);
    const startStr = start.toISOString().slice(0, 10);
    const endStr = end.toISOString().slice(0, 10);
    apiFetch<Analytics>(`/api/analytics?start_date=${startStr}&end_date=${endStr}`)
      .then(setAnalytics)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchForMonth(currentMonth);
  }, [currentMonth, fetchForMonth]);

  const goToPrevMonth = () => {
    setCurrentMonth((prev) => new Date(prev.getFullYear(), prev.getMonth() - 1, 1));
  };

  const goToNextMonth = () => {
    const next = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
    if (next <= new Date()) setCurrentMonth(next);
  };

  const isCurrentMonth = currentMonth.getMonth() === new Date().getMonth() && currentMonth.getFullYear() === new Date().getFullYear();

  if (loading && !analytics) return <p className="text-[var(--color-text-tertiary)]">Loading analytics...</p>;
  if (!analytics) return <p className="text-[var(--color-text-tertiary)]">Unable to load analytics</p>;

  const successRate = analytics.total_tasks > 0 ? (analytics.succeeded / analytics.total_tasks) * 100 : 0;
  const workflows = Object.entries(analytics.tasks_by_workflow)
    .map(([workflow, count]) => ({ workflow, count }))
    .sort((left, right) => right.count - left.count);
  const statuses = Object.entries(analytics.tasks_by_status)
    .map(([status, count]) => ({ status, count }))
    .sort((left, right) => right.count - left.count);
  const maxWorkflowCount = Math.max(...workflows.map((item) => item.count), 1);
  const maxDailyCount = Math.max(...analytics.daily_counts.map((item) => item.count), 1);

  /* Token per workflow - use dedicated field if available, otherwise fall back to tasks_by_workflow as proxy */
  const tokensByWorkflow = analytics.tokens_by_workflow
    ? Object.entries(analytics.tokens_by_workflow).map(([workflow, tokens]) => ({ workflow, tokens })).sort((a, b) => b.tokens - a.tokens)
    : workflows.map((w) => ({ workflow: w.workflow, tokens: 0 }));
  const maxTokens = Math.max(...tokensByWorkflow.map((i) => i.tokens), 1);
  const hasTokenData = tokensByWorkflow.some((i) => i.tokens > 0);

  return (
    <div>
      {/* Header with month navigation */}
      <div className="flex items-center justify-between mb-8">
        <h1 className="font-display text-3xl font-normal tracking-tight text-[var(--color-text-primary)]">Analytics</h1>
        <div className="flex items-center gap-3">
          <button
            onClick={goToPrevMonth}
            className="rounded-btn border border-ops-border bg-ops-surface px-3 py-1.5 text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] hover:border-[var(--color-accent)]/30 transition-all"
            aria-label="Previous month"
          >
            ←
          </button>
          <span className="text-sm font-medium text-[var(--color-text-primary)] min-w-[140px] text-center">
            {monthLabel(currentMonth)}
            {loading && <span className="ml-2 text-[var(--color-text-tertiary)]">…</span>}
          </span>
          <button
            onClick={goToNextMonth}
            disabled={isCurrentMonth}
            className="rounded-btn border border-ops-border bg-ops-surface px-3 py-1.5 text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] hover:border-[var(--color-accent)]/30 transition-all disabled:opacity-30 disabled:cursor-not-allowed"
            aria-label="Next month"
          >
            →
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">Total Tasks</p>
          <p className="text-3xl font-medium mt-2 text-[var(--color-text-primary)]">{analytics.total_tasks}</p>
        </div>
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">Success Rate</p>
          <p className="text-3xl font-medium mt-2 text-[var(--color-success)]">
            {successRate.toFixed(1)}%
          </p>
        </div>
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">Total Tokens</p>
          <p className="text-3xl font-medium mt-2 text-[var(--color-text-primary)]">{analytics.total_tokens.toLocaleString()}</p>
        </div>
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">Avg Duration</p>
          <p className="text-3xl font-medium mt-2 text-[var(--color-text-primary)]">
            {analytics.avg_duration_sec !== null ? `${Math.round(analytics.avg_duration_sec)}s` : 'N/A'}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Token Usage by Workflow (bar chart) */}
        <div className="bg-ops-surface border border-ops-border rounded-card p-6">
          <h3 className="text-base font-medium mb-5 text-[var(--color-text-primary)]">Token Usage by Workflow</h3>
          {hasTokenData ? (
            <div className="space-y-4">
              {tokensByWorkflow.map((item) => (
                <div key={item.workflow}>
                  <div className="flex justify-between text-sm mb-1.5">
                    <span className="text-[var(--color-text-secondary)] truncate mr-3">{item.workflow}</span>
                    <span className="text-[var(--color-text-tertiary)] tabular-nums flex-shrink-0">{item.tokens.toLocaleString()}</span>
                  </div>
                  <div className="w-full bg-ops-border rounded-full h-2">
                    <div
                      className="bg-[var(--color-accent)] h-2 rounded-full transition-all"
                      style={{ width: `${(item.tokens / maxTokens) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-[var(--color-text-tertiary)] text-sm">Token breakdown not available yet. Ensure backend returns <code className="text-xs">tokens_by_workflow</code>.</p>
          )}
        </div>

        {/* Tasks by Workflow */}
        <div className="bg-ops-surface border border-ops-border rounded-card p-6">
          <h3 className="text-base font-medium mb-5 text-[var(--color-text-primary)]">Tasks by Workflow</h3>
          <div className="space-y-4">
            {workflows.map((item) => (
              <div key={item.workflow}>
                <div className="flex justify-between text-sm mb-1.5">
                  <span className="text-[var(--color-text-secondary)]">{item.workflow}</span>
                  <span className="text-[var(--color-text-tertiary)] tabular-nums">{item.count.toLocaleString()}</span>
                </div>
                <div className="w-full bg-ops-border rounded-full h-1.5">
                  <div
                    className="bg-[var(--color-accent)] h-1.5 rounded-full transition-all"
                    style={{ width: `${(item.count / maxWorkflowCount) * 100}%` }}
                  />
                </div>
              </div>
            ))}
            {workflows.length === 0 && (
              <p className="text-[var(--color-text-tertiary)] text-sm">No data yet</p>
            )}
          </div>
        </div>

        {/* Daily Task Volume */}
        <div className="bg-ops-surface border border-ops-border rounded-card p-6">
          <h3 className="text-base font-medium mb-5 text-[var(--color-text-primary)]">Daily Task Volume</h3>
          <div className="space-y-4">
            {analytics.daily_counts.map((item) => (
              <div key={item.date}>
                <div className="flex justify-between text-sm mb-1.5">
                  <span className="text-[var(--color-text-secondary)]">{item.date}</span>
                  <span className="text-[var(--color-text-tertiary)] tabular-nums">{item.count}</span>
                </div>
                <div className="w-full bg-ops-border rounded-full h-1.5">
                  <div
                    className="bg-[var(--color-accent-secondary)] h-1.5 rounded-full transition-all"
                    style={{ width: `${(item.count / maxDailyCount) * 100}%` }}
                  />
                </div>
              </div>
            ))}
            {analytics.daily_counts.length === 0 && (
              <p className="text-[var(--color-text-tertiary)] text-sm">No data yet</p>
            )}
          </div>
        </div>

        {/* Tasks by Status */}
        <div className="bg-ops-surface border border-ops-border rounded-card p-6">
          <h3 className="text-base font-medium mb-5 text-[var(--color-text-primary)]">Task Breakdown</h3>
          <div className="space-y-4">
            {statuses.map((item) => {
              const pct = analytics.total_tasks > 0 ? (item.count / analytics.total_tasks) * 100 : 0;
              const color = {
                completed: 'bg-[var(--color-success)]',
                failed: 'bg-[var(--color-error)]',
                running: 'bg-[var(--color-warning)]',
                queued: 'bg-[var(--color-info)]',
                lost: 'bg-[var(--color-text-tertiary)]',
                succeeded: 'bg-[var(--color-success)]',
              }[item.status] || 'bg-[var(--color-text-tertiary)]';

              return (
                <div key={item.status}>
                  <div className="flex justify-between text-sm mb-1.5">
                    <span className="capitalize text-[var(--color-text-secondary)]">{item.status}</span>
                    <span className="text-[var(--color-text-tertiary)] tabular-nums">
                      {item.count} ({pct.toFixed(0)}%)
                    </span>
                  </div>
                  <div className="w-full bg-ops-border rounded-full h-1.5">
                    <div
                      className={`${color} h-1.5 rounded-full transition-all`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
            {statuses.length === 0 && (
              <p className="text-[var(--color-text-tertiary)] text-sm">No data yet</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
