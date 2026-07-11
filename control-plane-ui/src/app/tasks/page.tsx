'use client';

import { useEffect, useState, useCallback } from 'react';
import { apiFetch, Task, TaskArchiveResult, TaskDeleteResult, TaskListResult, TaskResetResult } from '@/lib/api';

type StatusFilter = 'all' | 'queued' | 'running' | 'waiting_approval' | 'waiting_user_input' | 'resume_pending' | 'succeeded' | 'failed' | 'lost' | 'timed_out';

const RERUNNABLE_STATUSES = new Set(['failed', 'lost', 'timed_out']);
const PAGE_SIZE = 50;

function formatDuration(durationSec: number | null): string {
  if (durationSec === null) {
    return '-';
  }

  if (durationSec < 60) {
    return `${Math.round(durationSec)}s`;
  }

  return `${(durationSec / 60).toFixed(1)}m`;
}

const CHANNEL_DISPLAY: Record<string, { label: string; icon: string }> = {
  salesforce: { label: 'Salesforce', icon: '☁' },
  servicenow: { label: 'ServiceNow', icon: '⚡' },
  message: { label: 'Message', icon: '💬' },
  schedule: { label: 'Schedule', icon: '⏱' },
  api: { label: 'API', icon: '⌘' },
};

function deriveOrigin(task: Task): { label: string; icon: string } {
  if (task.channel && CHANNEL_DISPLAY[task.channel]) {
    return CHANNEL_DISPLAY[task.channel];
  }
  if (task.channel) {
    return { label: task.channel, icon: '🔌' };
  }
  /* Fallback for rows without channel (pre-migration) */
  const meta = task.metadata as Record<string, unknown>;
  if (meta?.triggered_by === 'scheduler') return { label: 'Schedule', icon: '⏱' };
  const source = meta?.source;
  if (typeof source === 'string') {
    if (source.includes('servicenow')) return { label: 'ServiceNow', icon: '⚡' };
    if (source.includes('sf-email') || source.includes('salesforce')) return { label: 'Salesforce', icon: '☁' };
    return { label: source, icon: '🔌' };
  }
  if (task.message_channel) return { label: 'Message', icon: '💬' };
  return { label: 'API', icon: '⌘' };
}

export default function QueuePage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [filter, setFilter] = useState<StatusFilter>('all');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rerunningTaskId, setRerunningTaskId] = useState<string | null>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [archivingTaskId, setArchivingTaskId] = useState<string | null>(null);

  const fetchTasks = useCallback(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (filter !== 'all') params.set('status', filter);
    if (includeArchived) params.set('include_archived', 'true');
    apiFetch<TaskListResult>(`/api/tasks?${params.toString()}`)
      .then((result) => {
        setTasks(result.items);
        setTotal(result.total);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [filter, includeArchived, offset]);

  useEffect(() => {
    fetchTasks();
    const interval = setInterval(fetchTasks, 5000);
    return () => clearInterval(interval);
  }, [fetchTasks]);

  const statusColor = (s: string) => {
    switch (s) {
      case 'queued': return 'bg-[var(--color-info-muted)] text-[var(--color-info)]';
      case 'running': return 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]';
      case 'resume_pending': return 'bg-[var(--color-info-muted)] text-[var(--color-info)]';
      case 'waiting_approval':
      case 'waiting_user_input': return 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]';
      case 'succeeded': return 'bg-[var(--color-success-muted)] text-[var(--color-success)]';
      case 'failed': return 'bg-[var(--color-error-muted)] text-[var(--color-error)]';
      case 'lost': return 'bg-[var(--color-border-subtle)] text-[var(--color-text-tertiary)]';
      case 'timed_out': return 'bg-[var(--color-warning-muted)] text-[var(--color-warning)]';
      default: return 'bg-[var(--color-border-subtle)] text-[var(--color-text-tertiary)]';
    }
  };

  const rerunTask = useCallback(async (task: Task) => {
    setActionError(null);
    setRerunningTaskId(task.id);
    try {
      await apiFetch<TaskResetResult>(`/api/tasks/${task.id}/rerun`, { method: 'POST' });
      fetchTasks();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Failed to rerun task');
    } finally {
      setRerunningTaskId(null);
    }
  }, [fetchTasks]);

  const deleteTask = useCallback(async (task: Task) => {
    if (!window.confirm(`Delete task ${task.id.slice(0, 8)} and its related session data?`)) {
      return;
    }

    setActionError(null);
    setDeletingTaskId(task.id);
    try {
      await apiFetch<TaskDeleteResult>(`/api/tasks/${task.id}`, { method: 'DELETE' });
      fetchTasks();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Failed to delete task');
    } finally {
      setDeletingTaskId(null);
    }
  }, [fetchTasks]);

  const archiveTask = useCallback(async (task: Task) => {
    setActionError(null);
    setArchivingTaskId(task.id);
    try {
      const action = task.archived_at ? 'unarchive' : 'archive';
      await apiFetch<TaskArchiveResult>(`/api/tasks/${task.id}/${action}`, { method: 'POST' });
      fetchTasks();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Failed to update archive state');
    } finally {
      setArchivingTaskId(null);
    }
  }, [fetchTasks]);

  const setStatusFilter = useCallback((nextFilter: StatusFilter) => {
    setFilter(nextFilter);
    setOffset(0);
  }, []);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="font-display text-3xl font-normal tracking-tight text-[var(--color-text-primary)]">Tasks</h1>
        <div className="flex items-center gap-3">
          <label className="inline-flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => {
                setIncludeArchived(event.target.checked);
                setOffset(0);
              }}
              className="h-4 w-4 rounded border-ops-border bg-ops-surface"
            />
            Archived
          </label>
          <button
            onClick={fetchTasks}
            className="px-4 py-2 bg-ops-surface border border-ops-border rounded-btn text-sm text-[var(--color-text-secondary)] hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)] transition-all"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-1.5 mb-8">
        {(['all', 'queued', 'running', 'waiting_approval', 'waiting_user_input', 'resume_pending', 'succeeded', 'failed', 'lost', 'timed_out'] as StatusFilter[]).map((s) => {
          const label: Record<string, string> = { timed_out: 'Timed Out', waiting_approval: 'Approval', waiting_user_input: 'User Input', resume_pending: 'Resume' };
          return (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={`px-3 py-1.5 rounded-btn text-sm transition-all ${
              filter === s ? 'bg-[var(--color-accent)] text-white' : 'bg-ops-surface border border-ops-border text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)]'
            }`}
          >
            {label[s] || s.charAt(0).toUpperCase() + s.slice(1)}
          </button>);
        })}
      </div>

      {actionError && (
        <div className="mb-4 rounded-btn border border-[var(--color-error)]/30 bg-[var(--color-error-muted)] px-4 py-3 text-sm text-[var(--color-error)]">
          {actionError}
        </div>
      )}

      {/* Task Table */}
      {loading ? (
        <p className="text-[var(--color-text-tertiary)]">Loading tasks...</p>
      ) : (
        <div className="bg-ops-surface border border-ops-border rounded-card overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-ops-border text-left text-sm text-[var(--color-text-tertiary)]">
                <th className="px-4 py-3 font-medium">Task ID</th>
                <th className="px-4 py-3 font-medium">Workflow</th>
                <th className="px-4 py-3 font-medium">Origin</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Tokens</th>
                <th className="px-4 py-3 font-medium hidden sm:table-cell">Created</th>
                <th className="px-4 py-3 font-medium hidden md:table-cell">Duration</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const createdAt = new Date(task.created);
                const origin = deriveOrigin(task);

                return (
                  <tr key={task.id} className="border-b border-ops-border/50 hover:bg-ops-surface-raised/50 transition-colors">
                    <td className="px-4 py-3 font-mono text-sm text-[var(--color-text-secondary)]">{task.id.slice(0, 8)}</td>
                    <td className="px-4 py-3 text-sm text-[var(--color-text-primary)]">{task.workflow}</td>
                    <td className="px-4 py-3 text-sm text-[var(--color-text-tertiary)]">
                      <span className="inline-flex items-center gap-1" title={origin.label}>
                        <span className="text-xs">{origin.icon}</span>
                        <span className="hidden lg:inline">{origin.label}</span>
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${statusColor(task.status)}`}>{task.status}</span>
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--color-text-secondary)]">{task.tokens_used.toLocaleString()}</td>
                    <td className="px-4 py-3 text-sm text-[var(--color-text-tertiary)] hidden sm:table-cell">{createdAt.toLocaleString()}</td>
                    <td className="px-4 py-3 text-sm text-[var(--color-text-tertiary)] hidden md:table-cell">
                      {formatDuration(task.duration_sec)}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        {RERUNNABLE_STATUSES.has(task.status) && (
                          <ActionIconButton
                            label={rerunningTaskId === task.id ? 'Requeueing task' : 'Rerun task'}
                            title={rerunningTaskId === task.id ? 'Requeueing…' : 'Rerun task'}
                            tone="amber"
                            disabled={rerunningTaskId === task.id || deletingTaskId === task.id}
                            onClick={() => rerunTask(task)}
                          >
                            <ReplayIcon spinning={rerunningTaskId === task.id} />
                          </ActionIconButton>
                        )}
                        <ActionIconLink
                          label="View session"
                          title="View session"
                          tone="blue"
                          href={`/tasks/${task.id}`}
                        >
                          <EyeIcon />
                        </ActionIconLink>
                        <ActionIconButton
                          label={task.archived_at ? 'Unarchive task' : 'Archive task'}
                          title={task.archived_at ? 'Unarchive task' : 'Archive task'}
                          tone="blue"
                          disabled={archivingTaskId === task.id || deletingTaskId === task.id || rerunningTaskId === task.id}
                          onClick={() => archiveTask(task)}
                        >
                          <ArchiveIcon open={Boolean(task.archived_at)} spinning={archivingTaskId === task.id} />
                        </ActionIconButton>
                        <ActionIconButton
                          label={deletingTaskId === task.id ? 'Deleting task' : 'Delete task'}
                          title={deletingTaskId === task.id ? 'Deleting…' : 'Delete task'}
                          tone="rose"
                          disabled={deletingTaskId === task.id || rerunningTaskId === task.id}
                          onClick={() => deleteTask(task)}
                        >
                          <TrashIcon spinning={deletingTaskId === task.id} />
                        </ActionIconButton>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {tasks.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-[var(--color-text-tertiary)]">
                    No tasks found
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-4 flex items-center justify-between text-sm text-[var(--color-text-tertiary)]">
        <span>{total === 0 ? '0 tasks' : `${offset + 1}-${Math.min(offset + PAGE_SIZE, total)} of ${total}`}</span>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            className="px-3 py-1.5 rounded-btn border border-ops-border bg-ops-surface disabled:opacity-40"
          >
            Previous
          </button>
          <button
            type="button"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            className="px-3 py-1.5 rounded-btn border border-ops-border bg-ops-surface disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

function ActionIconButton({
  children,
  disabled,
  label,
  onClick,
  title,
  tone,
}: {
  children: React.ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  title: string;
  tone: 'blue' | 'amber' | 'rose';
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex h-9 w-9 items-center justify-center rounded-btn border transition-all hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-40 ${actionToneClasses[tone]}`}
    >
      {children}
    </button>
  );
}

function ActionIconLink({
  children,
  href,
  label,
  title,
  tone,
}: {
  children: React.ReactNode;
  href: string;
  label: string;
  title: string;
  tone: 'blue' | 'amber' | 'rose';
}) {
  return (
    <a
      aria-label={label}
      title={title}
      href={href}
      className={`inline-flex h-9 w-9 items-center justify-center rounded-btn border transition-all hover:-translate-y-0.5 ${actionToneClasses[tone]}`}
    >
      {children}
    </a>
  );
}

const actionToneClasses = {
  blue: 'border-[var(--color-info)]/20 bg-[var(--color-info-muted)] text-[var(--color-info)] hover:bg-[var(--color-info)]/20 hover:text-[var(--color-text-primary)]',
  amber: 'border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)] hover:bg-[var(--color-warning)]/20 hover:text-[var(--color-text-primary)]',
  rose: 'border-[var(--color-error)]/20 bg-[var(--color-error-muted)] text-[var(--color-error)] hover:bg-[var(--color-error)]/20 hover:text-[var(--color-text-primary)]',
} as const;

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
  );
}

function ReplayIcon({ spinning = false }: { spinning?: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={spinning ? 'animate-spin' : ''}>
      <polyline points="23 4 23 10 17 10"/>
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
    </svg>
  );
}

function TrashIcon({ spinning = false }: { spinning?: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={spinning ? 'animate-pulse' : ''}>
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
      <line x1="10" y1="11" x2="10" y2="17"/>
      <line x1="14" y1="11" x2="14" y2="17"/>
    </svg>
  );
}

function ArchiveIcon({ open = false, spinning = false }: { open?: boolean; spinning?: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={spinning ? 'animate-pulse' : ''}>
      <rect x="3" y="4" width="18" height="4" rx="1"/>
      <path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/>
      {open ? <path d="M9 14h6"/> : <path d="M10 12h4"/>}
    </svg>
  );
}
