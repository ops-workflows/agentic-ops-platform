'use client';

import { useEffect, useMemo, useState } from 'react';
import {
  Bot,
  Brain,
  Cable,
  CalendarClock,
  CircleDot,
  CircleUserRound,
  ChevronDown,
  ChevronRight,
  Clock3,
  Cloud,
  GitFork,
  MessageCircle,
  MessageSquare,
  PlugZap,
  Puzzle,
  Send,
  ServerCog,
  Sparkles,
  Terminal,
  Webhook,
  Wrench,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import { MarkdownPreview, SyntaxCode } from '@/components/content-preview';
import {
  apiFetch,
  type NodeKind,
  type SessionDetail,
  type Task,
  type TaskDeleteResult,
  type TaskResetResult,
  type TraceNode,
} from '@/lib/api';
import { useParams } from 'next/navigation';

const RERUNNABLE_STATUSES = new Set(['failed', 'lost', 'timed_out']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'lost', 'timed_out']);

/* ═══════════════════════════════════════════════════════════════════════════
   Formatters
   ═══════════════════════════════════════════════════════════════════════════ */

function fmt(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function fmtTime(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
}

function fmtEventTimestamp(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const date = d.toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
  return `${date.replace(/ (\d{4})$/, ', $1')} · ${fmtTime(value)}`;
}

function fmtDur(s: number | null | undefined): string {
  if (s == null) return '-';
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${Math.round(s)}s`;
  return `${(s / 60).toFixed(1)}m`;
}

type Origin = { label: string; icon: LucideIcon };

const CHANNEL_DISPLAY: Record<string, Origin> = {
  salesforce: { label: 'Salesforce', icon: Cloud },
  servicenow: { label: 'ServiceNow', icon: ServerCog },
  mattermost: { label: 'Mattermost', icon: MessageCircle },
  message: { label: 'Message', icon: MessageCircle },
  schedule: { label: 'Schedule', icon: CalendarClock },
  'gcp-pubsub': { label: 'GCP Pub/Sub', icon: Cable },
  api: { label: 'API', icon: Webhook },
};

function deriveOrigin(task: Task): Origin {
  if (task.channel && CHANNEL_DISPLAY[task.channel]) {
    return CHANNEL_DISPLAY[task.channel];
  }
  if (task.channel) {
    return { label: task.channel, icon: Cable };
  }
  const meta = task.metadata as Record<string, unknown>;
  if (meta?.triggered_by === 'scheduler')
    return { label: 'Schedule', icon: CalendarClock };
  const source = meta?.source;
  if (typeof source === 'string') {
    const normalizedSource = source.toLowerCase();
    if (normalizedSource.includes('servicenow'))
      return { label: 'ServiceNow', icon: ServerCog };
    if (source.includes('sf-email') || source.includes('salesforce'))
      return { label: 'Salesforce', icon: Cloud };
    if (normalizedSource.includes('mattermost'))
      return { label: 'Mattermost', icon: MessageCircle };
    if (normalizedSource.includes('schedule'))
      return { label: 'Schedule', icon: CalendarClock };
    return { label: source, icon: Cable };
  }
  if (task.message_channel) return { label: 'Message', icon: MessageCircle };
  return { label: 'API', icon: Webhook };
}

function formatJsonValue(value: unknown): string {
  if (value == null) return '-';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean')
    return String(value);
  return JSON.stringify(value);
}

function getJsonBody(input: string): string | undefined {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return undefined;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Visual configuration per node kind
   ═══════════════════════════════════════════════════════════════════════════ */

const KIND_CONFIG: Record<
  NodeKind,
  {
    icon: LucideIcon;
    accent: string;
    bg: string;
    border: string;
    badge?: string;
  }
> = {
  session: {
    icon: CircleDot,
    accent: 'text-[var(--color-info)]',
    bg: 'bg-[var(--color-info)]/8',
    border: 'border-[var(--color-info)]/20',
  },
  user: {
    icon: CircleUserRound,
    accent: 'text-[var(--color-accent)]',
    bg: 'bg-[var(--color-accent)]/8',
    border: 'border-[var(--color-accent)]/20',
    badge: 'USER',
  },
  assistant: {
    icon: Bot,
    accent: 'text-[#B9823A]',
    bg: 'bg-[#B9823A]/8',
    border: 'border-[#B9823A]/20',
    badge: 'ASSISTANT',
  },
  thinking: {
    icon: Brain,
    accent: 'text-[#818CF8]',
    bg: 'bg-[#818CF8]/8',
    border: 'border-[#818CF8]/20',
    badge: 'THINKING',
  },
  messaging: {
    icon: Send,
    accent: 'text-[var(--color-info)]',
    bg: 'bg-[var(--color-info)]/8',
    border: 'border-[var(--color-info)]/20',
    badge: 'AGENT MSG',
  },
  tool_call: {
    icon: Wrench,
    accent: 'text-[var(--color-warning)]',
    bg: 'bg-[var(--color-warning)]/8',
    border: 'border-[var(--color-warning)]/20',
    badge: 'TOOL',
  },
  tool_result: {
    icon: Terminal,
    accent: 'text-[var(--color-success)]',
    bg: 'bg-[var(--color-success)]/8',
    border: 'border-[var(--color-success)]/20',
    badge: 'RESULT',
  },
  subagent: {
    icon: GitFork,
    accent: 'text-[#A65A7A]',
    bg: 'bg-[#A65A7A]/8',
    border: 'border-[#A65A7A]/20',
    badge: 'SUBAGENT',
  },
  subagent_progress: {
    icon: Clock3,
    accent: 'text-[#A65A7A]/70',
    bg: 'bg-[#A65A7A]/5',
    border: 'border-[#A65A7A]/15',
  },
  hook: {
    icon: Puzzle,
    accent: 'text-[#C08A8A]',
    bg: 'bg-[#C08A8A]/8',
    border: 'border-[#C08A8A]/20',
    badge: 'HOOK',
  },
  lifecycle: {
    icon: Clock3,
    accent: 'text-[var(--color-text-tertiary)]',
    bg: 'bg-ops-surface',
    border: 'border-ops-border-subtle',
  },
  result: {
    icon: Terminal,
    accent: 'text-[var(--color-success)]',
    bg: 'bg-[var(--color-success)]/8',
    border: 'border-[var(--color-success)]/20',
    badge: 'RESULT',
  },
  error: {
    icon: XCircle,
    accent: 'text-[var(--color-error)]',
    bg: 'bg-[var(--color-error)]/10',
    border: 'border-[var(--color-error)]/25',
    badge: 'ERROR',
  },
};

function getNodeConfig(node: TraceNode) {
  const base = KIND_CONFIG[node.kind];
  if (node.isError)
    return {
      ...base,
      accent: 'text-[var(--color-error)]',
      bg: 'bg-[var(--color-error)]/10',
      border: 'border-[var(--color-error)]/25',
    };
  return base;
}

function getTraceIcon(node: TraceNode): LucideIcon {
  if (node.badge === 'REQUEST') return MessageSquare;
  if (node.badge === 'AGENT' || node.badge === 'ASSISTANT') return Bot;
  if (node.badge === 'MCP') return PlugZap;
  if (node.badge === 'SKILL') return Sparkles;
  return getNodeConfig(node).icon;
}

function getBadgeTextClass(badge: string, isError: boolean): string {
  if (isError) return 'text-[var(--color-error)]';

  const badgeTextClasses: Record<string, string> = {
    AGENT: 'text-[var(--color-info)]',
    'AGENT MSG': 'text-[var(--color-info)]',
    ASSISTANT: 'text-[#B9823A]',
    ERROR: 'text-[var(--color-error)]',
    HOOK: 'text-[#C08A8A]',
    MCP: 'text-[#38BDF8]',
    REQUEST: 'text-[#60A5FA]',
    RESULT: 'text-[var(--color-success)]',
    SKILL: 'text-[#A78BCA]',
    SUBAGENT: 'text-[#A65A7A]',
    THINKING: 'text-[#818CF8]',
    TOOL: 'text-[var(--color-warning)]',
    USER: 'text-[var(--color-accent)]',
  };

  return badgeTextClasses[badge] ?? 'text-[var(--color-text-tertiary)]';
}

function getBadgeClasses(badge: string, isError: boolean): string {
  return `border-current/40 ${getBadgeTextClass(badge, isError)}`;
}

function JsonCode({
  body,
  compact = false,
}: {
  body: string;
  compact?: boolean;
}) {
  return (
    <SyntaxCode
      code={body}
      language="json"
      className={`overflow-auto ${compact ? 'max-h-48 px-0 text-[10px]' : 'max-h-[400px] p-3 text-[13px]'} leading-relaxed`}
    />
  );
}

function TraceBody({ body }: { body: string }) {
  const json = getJsonBody(body);

  if (json) {
    return <JsonCode body={json} />;
  }

  return (
    <MarkdownPreview
      body={body}
      className="max-h-[400px] overflow-auto px-3 py-2"
    />
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tree node component — recursive, collapsible
   ═══════════════════════════════════════════════════════════════════════════ */

function TraceNodeView({
  node,
  depth = 0,
  forceOpen,
  showThinking,
}: {
  node: TraceNode;
  depth?: number;
  forceOpen?: boolean | null;
  showThinking?: boolean;
}) {
  const visibleChildren = useMemo(
    () =>
      (node.children || []).filter(
        (c) => showThinking || c.kind !== 'thinking',
      ),
    [node.children, showThinking],
  );
  const hasChildren = visibleChildren.length > 0;
  const hasBody = Boolean(node.body);
  const defaultState = () => {
    return (
      node.badge === 'REQUEST' ||
      (node.meta?.result_role === 'session_result' && node.label === 'Final')
    );
  };

  const [open, setOpen] = useState(defaultState);

  useEffect(() => {
    if (forceOpen === true) setOpen(true);
    else if (forceOpen === false) setOpen(false);
  }, [forceOpen]);

  if (node.kind === 'thinking' && !showThinking) {
    return null;
  }

  const cfg = getNodeConfig(node);
  const Icon = getTraceIcon(node);
  const badge = node.badge ?? cfg.badge;
  const iconAccent = badge
    ? getBadgeTextClass(badge, Boolean(node.isError))
    : cfg.accent;
  const isExpandable = hasChildren || hasBody;
  const ExpandIcon = open ? ChevronDown : ChevronRight;
  const regularBorder = 'border-ops-border';

  return (
    <div className={`relative min-w-0 ${depth > 0 ? 'pl-6' : ''}`}>
      {depth > 0 && (
        <div className="absolute left-[11px] top-[14px] h-px w-3 bg-ops-border" />
      )}
      <div className="relative min-w-0">
        {/* Node row */}
        <div
          className={`group flex min-w-0 items-start gap-2 rounded-btn px-3 py-2 transition-all duration-150
            ${isExpandable ? 'cursor-pointer hover:bg-ops-surface-raised/50' : ''} ${cfg.bg} border ${regularBorder}`}
          onClick={() => {
            if (isExpandable) setOpen(!open);
          }}
        >
          <span
            className={`mt-0.5 flex h-3 w-3 flex-none items-center justify-center ${iconAccent}`}
          >
            <Icon aria-hidden="true" size={13} strokeWidth={1.8} />
          </span>
          {badge && (
            <span
              className={`mt-px flex-none rounded border px-1.5 py-0.5 text-[9px] font-bold tracking-[0.15em] uppercase ${getBadgeClasses(badge, Boolean(node.isError))}`}
            >
              {badge}
            </span>
          )}
          <span
            className={`min-w-0 truncate font-medium text-sm leading-tight ${node.isError ? 'text-[var(--color-error)]' : 'text-[var(--color-text-primary)]'}`}
            title={node.label}
          >
            {node.label}
          </span>
          <span className="ml-auto mt-0.5 hidden flex-none pl-2 text-[10px] tabular-nums text-[var(--color-text-tertiary)] sm:inline">
            {fmtEventTimestamp(node.timestamp)}
          </span>
          {isExpandable && (
            <span
              className={`mt-0.5 flex h-3 w-3 flex-none items-center justify-center ${iconAccent}`}
            >
              <ExpandIcon aria-hidden="true" size={13} strokeWidth={1.8} />
            </span>
          )}
        </div>

        {/* Body */}
        {node.body && open && (
          <div
            className={`ml-5 mt-1 mb-2 rounded-btn border ${regularBorder} bg-ops-bg overflow-hidden`}
          >
            <TraceBody body={node.body} />
          </div>
        )}

        {/* Children */}
        {open && hasChildren && (
          <div
            className={`relative mt-1 space-y-1 ${visibleChildren.length > 1 ? 'before:absolute before:top-[14px] before:bottom-[14px] before:left-[11px] before:w-px before:bg-ops-border' : ''}`}
          >
            {visibleChildren.map((child) => (
              <TraceNodeView
                key={child.id}
                node={child}
                depth={depth + 1}
                forceOpen={forceOpen}
                showThinking={showThinking}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Stat pill
   ═══════════════════════════════════════════════════════════════════════════ */

function StatPill({
  label,
  value,
  accent,
}: {
  label: string;
  value: string | number;
  accent?: string;
}) {
  return (
    <div className="flex items-baseline gap-2 rounded-card border border-ops-border bg-ops-surface px-4 py-3">
      <span
        className={`text-2xl font-medium tabular-nums ${accent ?? 'text-[var(--color-text-primary)]'}`}
      >
        {value}
      </span>
      <span className="text-[10px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">
        {label}
      </span>
    </div>
  );
}

function formatCompactTokenTotal(total: number | string): string {
  if (typeof total === 'string') {
    return total;
  }

  if (total < 1000) {
    return String(total);
  }

  if (total < 1_000_000) {
    return `${(total / 1000).toFixed(1)}K`;
  }

  return `${(total / 1_000_000).toFixed(1)}M`;
}

function TokenStat({
  total,
  input,
  output,
}: {
  total: number | string;
  input?: string;
  output?: string;
}) {
  const hasTooltip = true;
  const totalDisplay = formatCompactTokenTotal(total);

  return (
    <div className={`group relative ${hasTooltip ? 'cursor-help' : ''}`}>
      <div className="flex items-baseline gap-2 rounded-card border border-ops-border bg-ops-surface px-4 py-3">
        <span className="text-2xl font-medium tabular-nums text-[var(--color-accent)]">
          {totalDisplay}
        </span>
        <span className="text-[10px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">
          Usage
        </span>
      </div>

      {hasTooltip && (
        <div className="pointer-events-none absolute right-0 top-full z-20 mt-2 w-max min-w-[180px] max-w-[calc(100vw-2rem)] rounded-btn border border-ops-border bg-ops-surface-raised px-3 py-2 opacity-0 shadow-card-hover transition-opacity duration-150 group-hover:opacity-100">
          {input && output ? (
            <>
              <div className="flex items-baseline gap-2 text-xs">
                <span className="font-semibold uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
                  IN:
                </span>
                <span className="font-mono text-[var(--color-text-primary)]">
                  {input}
                </span>
              </div>
              <div className="mt-1 flex items-baseline gap-2 text-xs">
                <span className="font-semibold uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
                  OUT:
                </span>
                <span className="font-mono text-[var(--color-text-primary)]">
                  {output}
                </span>
              </div>
            </>
          ) : (
            <div className="text-xs font-medium uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
              Observed total only
            </div>
          )}
          <p className="mt-2 max-w-[240px] text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
            Cumulative observed token usage across the coordinator and
            subagents. This is not context-window occupancy.
          </p>
        </div>
      )}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-btn border border-ops-border bg-ops-surface px-3 py-2">
      <p className="text-[9px] uppercase tracking-[0.15em] text-[var(--color-text-tertiary)]">
        {label}
      </p>
      <p className="text-lg font-medium text-[var(--color-text-primary)] tabular-nums">
        {value}
      </p>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Page component
   ═══════════════════════════════════════════════════════════════════════════ */

export default function SessionDetailPage() {
  const params = useParams();
  const taskIdParam = params?.taskId;
  const taskId = Array.isArray(taskIdParam)
    ? taskIdParam[0]
    : (taskIdParam ?? '');
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [taskRecord, setTaskRecord] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showThinking, setShowThinking] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [forceOpen, setForceOpen] = useState<boolean | null>(null);

  useEffect(() => {
    if (!taskId) {
      setLoading(false);
      return;
    }
    let cancelled = false;

    const doFetch = async () => {
      try {
        const task = await apiFetch<Task>(`/api/tasks/${taskId}`);
        if (cancelled) return;
        setTaskRecord(task);

        // Disable auto-refresh once the task is terminal so we stop polling.
        if (TERMINAL_STATUSES.has(task.status)) {
          setAutoRefresh(false);
        }

        if (task.status === 'queued') {
          setSession(null);
          return;
        }

        try {
          const detail = await apiFetch<SessionDetail>(
            `/api/sessions/${taskId}`,
          );
          if (cancelled) return;
          setSession(detail);
        } catch {
          if (cancelled) return;
          setSession(null);
        }
      } catch (error) {
        if (cancelled) return;
        console.error('Failed to load task detail:', error);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    doFetch();
    if (autoRefresh) {
      const interval = setInterval(doFetch, 3000);
      return () => {
        cancelled = true;
        clearInterval(interval);
      };
    }
    return () => {
      cancelled = true;
    };
  }, [taskId, autoRefresh]);

  if (loading)
    return (
      <p className="text-[var(--color-text-tertiary)] py-20 text-center">
        Loading session…
      </p>
    );
  if (!taskId)
    return (
      <p className="text-[var(--color-text-tertiary)] py-20 text-center">
        Invalid session route
      </p>
    );
  if (!taskRecord && !session)
    return (
      <p className="text-[var(--color-text-tertiary)] py-20 text-center">
        Task not found
      </p>
    );
  const task = session?.task ?? taskRecord;
  if (!task)
    return (
      <p className="text-[var(--color-text-tertiary)] py-20 text-center">
        Task details not available
      </p>
    );

  const rerunTask = async () => {
    if (!RERUNNABLE_STATUSES.has(task.status)) return;
    setActionError(null);
    setRerunning(true);
    try {
      await apiFetch<TaskResetResult>(`/api/tasks/${task.id}/rerun`, {
        method: 'POST',
      });
      window.location.href = '/tasks';
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : 'Failed to rerun task',
      );
      setRerunning(false);
    }
  };

  const deleteTask = async () => {
    if (
      !window.confirm(
        `Delete task ${task.id.slice(0, 8)} and its related session data?`,
      )
    )
      return;
    setActionError(null);
    setDeleting(true);
    try {
      await apiFetch<TaskDeleteResult>(`/api/tasks/${task.id}`, {
        method: 'DELETE',
      });
      window.location.href = '/tasks';
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : 'Failed to delete task',
      );
      setDeleting(false);
    }
  };

  const statusStyles: Record<string, string> = {
    queued:
      'text-[var(--color-info)] bg-[var(--color-info)]/10 border-[var(--color-info)]/20',
    running:
      'text-[var(--color-warning)] bg-[var(--color-warning)]/10 border-[var(--color-warning)]/20',
    succeeded:
      'text-[var(--color-success)] bg-[var(--color-success)]/10 border-[var(--color-success)]/20',
    failed:
      'text-[var(--color-error)] bg-[var(--color-error)]/10 border-[var(--color-error)]/20',
    lost: 'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border',
    timed_out:
      'text-[var(--color-warning)] bg-[var(--color-warning)]/10 border-[var(--color-warning)]/20',
  };

  const origin = deriveOrigin(task);

  if (!session && task.status === 'queued') {
    const metadataEntries = Object.entries(task.metadata ?? {});

    return (
      <div className="space-y-5">
        <header className="rounded-card border border-ops-border bg-ops-surface p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <div className="flex items-center gap-3">
                <h1 className="font-display text-2xl font-normal tracking-tight text-[var(--color-text-primary)]">
                  Queued Task
                </h1>
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.1em] ${statusStyles[task.status] ?? 'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border'}`}
                >
                  {task.status}
                </span>
              </div>
              <p className="text-sm text-[var(--color-text-secondary)]">
                {task.workflow}
              </p>
              <p className="font-mono text-[10px] text-[var(--color-text-tertiary)]">
                {task.id}
              </p>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 pt-1 text-xs text-[var(--color-text-tertiary)]">
                <span className="inline-flex items-center gap-2">
                  <span className="uppercase tracking-[0.15em]">Origin</span>
                  <span className="inline-flex items-center gap-1.5 text-sm text-[var(--color-text-secondary)]">
                    <origin.icon
                      aria-hidden="true"
                      size={15}
                      strokeWidth={1.8}
                    />
                    {origin.label}
                  </span>
                </span>
                <span className="inline-flex items-center gap-2">
                  <span className="uppercase tracking-[0.15em]">Created</span>
                  <span className="text-sm text-[var(--color-text-secondary)]">
                    {fmt(task.created)}
                  </span>
                </span>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <HeaderActionButton
                label={deleting ? 'Deleting task' : 'Delete task'}
                title={deleting ? 'Deleting…' : 'Delete task'}
                tone="rose"
                disabled={deleting || rerunning}
                onClick={deleteTask}
              >
                <TrashIcon spinning={deleting} />
              </HeaderActionButton>
              <label className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)] select-none cursor-pointer">
                <input
                  type="checkbox"
                  checked={autoRefresh}
                  onChange={(e) => setAutoRefresh(e.target.checked)}
                  className="rounded border-ops-border bg-transparent accent-[var(--color-accent)]"
                />
                Auto-refresh
              </label>
            </div>
          </div>

          {actionError && (
            <div className="mt-4 rounded-btn border border-[var(--color-error)]/30 bg-[var(--color-error)]/10 px-4 py-3 text-sm text-[var(--color-error)]">
              {actionError}
            </div>
          )}
        </header>

        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
          <section className="rounded-card border border-ops-border bg-ops-surface p-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
                Request
              </h2>
              <span className="text-xs text-[var(--color-text-tertiary)]">
                Waiting for session start
              </span>
            </div>
            <div className="rounded-btn border border-ops-border-subtle bg-ops-bg overflow-hidden">
              <pre className="p-4 text-[13px] leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap break-words font-[inherit] max-h-[520px] overflow-auto">
                {task.prompt || 'No prompt stored for this task.'}
              </pre>
            </div>
          </section>

          <aside className="space-y-4">
            <section className="rounded-card border border-ops-border bg-ops-surface p-4">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
                Task Metadata
              </h3>
              <p className="mb-3 text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">
                This task is still in queue, so there is no session trace yet.
                The page shows the queued work item and its routing metadata
                until execution begins.
              </p>
              {metadataEntries.length > 0 ? (
                <div className="space-y-2">
                  {metadataEntries.map(([key, value]) => (
                    <div
                      key={key}
                      className="rounded-btn border border-ops-border-subtle bg-ops-bg px-3 py-2"
                    >
                      <p className="text-[9px] uppercase tracking-[0.15em] text-[var(--color-text-tertiary)]">
                        {key}
                      </p>
                      <p className="mt-1 text-xs leading-relaxed text-[var(--color-text-secondary)] break-words">
                        {formatJsonValue(value)}
                      </p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[var(--color-text-tertiary)]">
                  No metadata captured for this task.
                </p>
              )}
            </section>
          </aside>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <p className="text-[var(--color-text-tertiary)] py-20 text-center">
        Session has not started yet.
      </p>
    );
  }

  const sessionDetail = session;
  const trace = sessionDetail.trace;
  const root = trace?.root ?? {
    id: 'root',
    kind: 'session' as const,
    timestamp: new Date().toISOString(),
    label: 'Trace',
    children: [],
  };
  const stats = trace?.stats ?? {
    toolCalls: 0,
    toolErrors: 0,
    assistantMessages: 0,
    subagentSpawns: 0,
    totalTurns: 0,
    tokensIn: 0,
    tokensOut: 0,
  };
  const heartbeats = trace?.heartbeats ?? [];
  const skillsUsed = trace?.skillsUsed ?? [];
  const mcpsUsed = trace?.mcpsUsed ?? [];
  const eventCount = trace?.eventCount ?? 0;

  const finalTokenTotal =
    (sessionDetail.tokens_input ?? 0) + (sessionDetail.tokens_output ?? 0);
  const totalTokens = Math.max(finalTokenTotal, task.tokens_used);
  const hasFinalTokenTotals = finalTokenTotal > 0;
  const inputTokensDisplay = hasFinalTokenTotals
    ? (sessionDetail.tokens_input ?? 0).toLocaleString()
    : '-';
  const outputTokensDisplay = hasFinalTokenTotals
    ? (sessionDetail.tokens_output ?? 0).toLocaleString()
    : '-';
  const totalTokensDisplay =
    totalTokens > 0
      ? totalTokens
      : task.status === 'lost' || sessionDetail.status === 'running'
        ? 'n/a'
        : '0';
  const avgHeartbeatGap =
    heartbeats.length > 1
      ? Math.round(
          heartbeats.slice(1).reduce((sum, hb, idx) => {
            const cur = new Date(hb.timestamp as string).getTime();
            const prev = new Date(
              heartbeats[idx].timestamp as string,
            ).getTime();
            return sum + (cur - prev) / 1000;
          }, 0) /
            (heartbeats.length - 1),
        )
      : null;

  return (
    <div className="space-y-5">
      {/* ── Header ── */}
      <header className="rounded-card border border-ops-border bg-ops-surface p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-3">
              <h1 className="font-display text-2xl font-normal tracking-tight text-[var(--color-text-primary)]">
                Session Trace
              </h1>
              <span
                className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.1em] ${statusStyles[task.status] ?? 'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border'}`}
              >
                {task.status}
              </span>
              {task.status === 'running' && (
                <span className="inline-flex h-2 w-2 rounded-full bg-[var(--color-warning)] animate-pulse" />
              )}
            </div>
            <p className="text-sm text-[var(--color-text-secondary)]">
              {task.workflow}
            </p>
            <p className="font-mono text-[10px] text-[var(--color-text-tertiary)]">
              {taskId}
            </p>
            <div className="flex items-center gap-2 pt-1 text-xs text-[var(--color-text-tertiary)]">
              <span className="uppercase tracking-[0.15em]">Origin</span>
              <span className="inline-flex items-center gap-1.5 text-sm text-[var(--color-text-secondary)]">
                <origin.icon aria-hidden="true" size={15} strokeWidth={1.8} />
                {origin.label}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {RERUNNABLE_STATUSES.has(task.status) && (
              <HeaderActionButton
                label={rerunning ? 'Requeueing task' : 'Rerun task'}
                title={rerunning ? 'Requeueing…' : 'Rerun task'}
                tone="amber"
                disabled={rerunning || deleting}
                onClick={rerunTask}
              >
                <ReplayIcon spinning={rerunning} />
              </HeaderActionButton>
            )}
            <HeaderActionButton
              label={deleting ? 'Deleting task' : 'Delete task'}
              title={deleting ? 'Deleting…' : 'Delete task'}
              tone="rose"
              disabled={deleting || rerunning}
              onClick={deleteTask}
            >
              <TrashIcon spinning={deleting} />
            </HeaderActionButton>
            <label className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)] select-none cursor-pointer">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="rounded border-ops-border bg-transparent accent-[var(--color-accent)]"
              />
              Auto-refresh
            </label>
          </div>
        </div>

        {actionError && (
          <div className="mt-4 rounded-btn border border-[var(--color-error)]/30 bg-[var(--color-error)]/10 px-4 py-3 text-sm text-[var(--color-error)]">
            {actionError}
          </div>
        )}

        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-6">
          <StatPill
            label="Tool Calls"
            value={stats.toolCalls}
            accent="text-[var(--color-warning)]"
          />
          <StatPill
            label="Errors"
            value={stats.toolErrors}
            accent={
              stats.toolErrors > 0 ? 'text-[var(--color-error)]' : undefined
            }
          />
          <StatPill
            label="Messages"
            value={stats.assistantMessages}
            accent="text-[var(--color-info)]"
          />
          <StatPill
            label="Subagents"
            value={stats.subagentSpawns}
            accent="text-[#A65A7A]"
          />
          <StatPill label="Heartbeats" value={heartbeats.length} />
          <TokenStat
            total={totalTokensDisplay}
            input={hasFinalTokenTotals ? inputTokensDisplay : undefined}
            output={hasFinalTokenTotals ? outputTokensDisplay : undefined}
          />
        </div>
      </header>

      {/* ── Main ── */}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
        <section className="min-w-0 rounded-card border border-ops-border bg-ops-surface p-4">
          <div className="mb-4 flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
                {root.label}
              </h2>
              {root.label !== 'Trace' && (
                <span
                  className={`rounded border px-1.5 py-0.5 text-[9px] font-bold tracking-[0.15em] uppercase ${getBadgeClasses('AGENT', false)}`}
                >
                  Agent
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1.5 rounded-btn border border-ops-border bg-ops-surface px-2 py-1 text-[10px] text-[var(--color-text-tertiary)] select-none cursor-pointer">
                <input
                  type="checkbox"
                  checked={showThinking}
                  onChange={(e) => setShowThinking(e.target.checked)}
                  className="rounded border-ops-border bg-transparent accent-[var(--color-accent)]"
                />
                Reasoning
              </label>
              <div className="flex rounded-btn border border-ops-border bg-ops-surface text-[10px]">
                <button
                  onClick={() => setForceOpen(true)}
                  className="px-2 py-1 text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] transition-colors"
                >
                  Expand all
                </button>
                <button
                  onClick={() => setForceOpen(false)}
                  className="px-2 py-1 text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] transition-colors border-l border-ops-border"
                >
                  Collapse
                </button>
              </div>
            </div>
          </div>

          <div className="space-y-1">
            {root.children.length === 0 ? (
              <p className="py-16 text-center text-[var(--color-text-tertiary)]">
                No trace entries yet
              </p>
            ) : (
              root.children.map((child) => (
                <TraceNodeView
                  key={child.id}
                  node={child}
                  depth={0}
                  forceOpen={forceOpen}
                  showThinking={showThinking}
                />
              ))
            )}
          </div>
        </section>

        {/* Sidebar */}
        <aside className="min-w-0 space-y-4">
          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
              Runtime
            </h3>
            <div className="grid grid-cols-2 gap-2">
              <MiniStat
                label="Duration"
                value={fmtDur(sessionDetail.duration_sec ?? task.duration_sec)}
              />
              <MiniStat
                label="Turns"
                value={sessionDetail.turns || stats.totalTurns || '-'}
              />
              <MiniStat
                label="HB Gap"
                value={avgHeartbeatGap != null ? `${avgHeartbeatGap}s` : '-'}
              />
              <MiniStat label="Events" value={eventCount} />
            </div>
          </section>

          {sessionDetail.subagents_used &&
            sessionDetail.subagents_used.length > 0 && (
              <section className="rounded-card border border-ops-border bg-ops-surface p-4">
                <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
                  Subagents
                </h3>
                <div className="space-y-2">
                  {sessionDetail.subagents_used.map((sa) => (
                    <div
                      key={sa.name}
                      className="flex items-center justify-between rounded-btn border border-[#A65A7A]/15 bg-[#A65A7A]/5 px-3 py-2"
                    >
                      <span className="text-xs font-medium text-[#A65A7A]">
                        {sa.name}
                      </span>
                      <span className="text-[10px] text-[var(--color-text-tertiary)]">
                        {sa.turns}t · {sa.tokens}tok
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
              Skills
            </h3>
            {skillsUsed.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {skillsUsed.map((skill) => (
                  <span
                    key={skill}
                    className="rounded-full border border-[var(--color-accent)]/20 bg-[var(--color-accent-muted)] px-3 py-1 text-xs text-[var(--color-accent)]"
                  >
                    {skill}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-[var(--color-text-tertiary)]">
                No explicit Skill tool invocation was recorded in this session.
              </p>
            )}
          </section>

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
              MCPs
            </h3>
            {mcpsUsed.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {mcpsUsed.map((mcp) => (
                  <span
                    key={mcp}
                    className="rounded-full border border-[var(--color-info)]/20 bg-[var(--color-info)]/8 px-3 py-1 text-xs text-[var(--color-info)]"
                  >
                    {mcp}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-[var(--color-text-tertiary)]">
                No MCP tool was invoked in this session.
              </p>
            )}
          </section>

          {sessionDetail.tools_used && sessionDetail.tools_used.length > 0 && (
            <section className="rounded-card border border-ops-border bg-ops-surface p-4">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
                Top Tools
              </h3>
              <div className="space-y-1.5">
                {sessionDetail.tools_used.slice(0, 8).map((t) => (
                  <div
                    key={t.name}
                    className="flex items-center justify-between text-xs"
                  >
                    <span
                      className="text-[var(--color-text-secondary)] truncate max-w-[160px]"
                      title={t.name}
                    >
                      {t.name}
                    </span>
                    <span className="text-[var(--color-text-tertiary)] tabular-nums">
                      {t.count}×
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)]">
                Heartbeats
              </h3>
              <span className="text-[10px] text-[var(--color-text-tertiary)]">
                {heartbeats.length}
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {heartbeats.slice(-60).map((hb, idx, arr) => (
                <span
                  key={(hb.id as string) || idx}
                  className={`h-1.5 w-1.5 rounded-full ${
                    TERMINAL_STATUSES.has(task.status)
                      ? 'bg-ops-border'
                      : idx === arr.length - 1
                        ? 'bg-[var(--color-success)]'
                        : 'bg-ops-border'
                  }`}
                  title={fmt((hb.timestamp as string) || '')}
                />
              ))}
              {heartbeats.length === 0 && (
                <p className="text-[10px] text-[var(--color-text-tertiary)]">
                  None recorded
                </p>
              )}
            </div>
          </section>

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">
              Task Metadata
            </h3>
            <JsonCode
              body={JSON.stringify(task.metadata ?? {}, null, 2)}
              compact
            />
          </section>
        </aside>
      </div>
    </div>
  );
}

function HeaderActionButton({
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
  tone: 'amber' | 'rose';
}) {
  const toneClasses = {
    amber:
      'border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 text-[var(--color-warning)] hover:bg-[var(--color-warning)]/15 hover:text-[var(--color-text-primary)]',
    rose: 'border-[var(--color-error)]/30 bg-[var(--color-error)]/10 text-[var(--color-error)] hover:bg-[var(--color-error)]/15 hover:text-[var(--color-text-primary)]',
  } as const;

  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex h-10 w-10 items-center justify-center rounded-btn border transition-all hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-40 ${toneClasses[tone]}`}
    >
      {children}
    </button>
  );
}

function ReplayIcon({ spinning = false }: { spinning?: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={spinning ? 'animate-spin' : ''}
    >
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </svg>
  );
}

function TrashIcon({ spinning = false }: { spinning?: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={spinning ? 'animate-pulse' : ''}
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  );
}
