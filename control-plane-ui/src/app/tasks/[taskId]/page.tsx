'use client';

import { useEffect, useMemo, useState, useCallback } from 'react';
import { apiFetch, SessionDetail, SessionEvent, Task, TaskDeleteResult, TaskResetResult } from '@/lib/api';
import { useParams } from 'next/navigation';

const RERUNNABLE_STATUSES = new Set(['failed', 'lost', 'timed_out']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'lost', 'timed_out']);

/* ═══════════════════════════════════════════════════════════════════════════
   Types — hierarchical trace tree
   ═══════════════════════════════════════════════════════════════════════════ */

type NodeKind =
  | 'session'
  | 'user'
  | 'assistant'
  | 'tool_call'
  | 'tool_result'
  | 'subagent'
  | 'subagent_progress'
  | 'hook'
  | 'lifecycle'
  | 'result'
  | 'error';

interface TraceNode {
  id: string;
  kind: NodeKind;
  timestamp: string;
  label: string;
  detail?: string;
  body?: string;
  duration?: number;
  isError?: boolean;
  children: TraceNode[];
  raw?: unknown;
  meta?: Record<string, string>;
}

interface SessionStats {
  toolCalls: number;
  toolErrors: number;
  assistantMessages: number;
  subagentSpawns: number;
  totalTurns: number;
  tokensIn: number;
  tokensOut: number;
}

/* ═══════════════════════════════════════════════════════════════════════════
   Formatters
   ═══════════════════════════════════════════════════════════════════════════ */

function fmt(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function fmtTime(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDur(s: number | null | undefined): string {
  if (s == null) return '-';
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${Math.round(s)}s`;
  return `${(s / 60).toFixed(1)}m`;
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

function formatJsonValue(value: unknown): string {
  if (value == null) return '-';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function epochToIso(v: unknown, fallback: string): string {
  return typeof v === 'number' ? new Date(v * 1000).toISOString() : fallback;
}

function flatText(content: unknown): string {
  if (!Array.isArray(content)) return '';
  return content
    .filter((b): b is Record<string, unknown> => !!b && typeof b === 'object')
    .filter((b) => b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text as string)
    .join('\n\n')
    .trim();
}

function parseAgentPreview(input: string): { name: string; detail?: string } {
  let name = 'subagent';
  let detail: string | undefined;

  try {
    const parsed = JSON.parse(input) as Record<string, unknown>;
    name = typeof parsed.subagent_type === 'string'
      ? parsed.subagent_type
      : typeof parsed.description === 'string'
        ? parsed.description
        : name;
    detail = typeof parsed.description === 'string' ? parsed.description : undefined;
    return { name, detail };
  } catch {
    const subagentMatch = input.match(/"subagent_type"\s*:\s*"([^"]+)"/);
    const descriptionMatch = input.match(/"description"\s*:\s*"([^"]+)"/);
    return {
      name: subagentMatch?.[1] ?? descriptionMatch?.[1] ?? name,
      detail: descriptionMatch?.[1],
    };
  }
}

function parseSkillPreview(input: string): string | undefined {
  try {
    const parsed = JSON.parse(input) as Record<string, unknown>;
    return typeof parsed.skill === 'string' ? parsed.skill : undefined;
  } catch {
    return input.match(/"skill"\s*:\s*"([^"]+)"/)?.[1];
  }
}

function summarizeTaskType(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value.trim()) return undefined;
  return value.replace(/_/g, ' ');
}

function isDuplicateNarrative(left?: string, right?: string): boolean {
  const normalizedLeft = left?.trim();
  const normalizedRight = right?.trim();
  if (!normalizedLeft || !normalizedRight) return false;
  if (normalizedLeft === normalizedRight) return true;
  const prefixLength = Math.min(normalizedLeft.length, normalizedRight.length, 280);
  return normalizedLeft.slice(0, prefixLength) === normalizedRight.slice(0, prefixLength);
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tree builder — converts flat events into hierarchical trace
   ═══════════════════════════════════════════════════════════════════════════ */

function buildTree(events: SessionEvent[], fullPrompt?: string): { root: TraceNode; stats: SessionStats; heartbeats: SessionEvent[]; skillsUsed: string[]; mcpsUsed: string[]; coordinatorAgent?: string } {
  const root: TraceNode = {
    id: 'root',
    kind: 'session',
    timestamp: events[0]?.timestamp ?? new Date().toISOString(),
    label: 'Session',
    children: [],
  };

  const heartbeats: SessionEvent[] = [];
  const toolNameMap = new Map<string, string>();
  const pendingToolCalls = new Map<string, TraceNode>();
  const subagentsByTaskId = new Map<string, TraceNode>();
  // Subagent node keyed by the Task/Agent tool_use id that spawned it. Subagent
  // messages carry that id in parent_tool_use_id, letting us nest their real
  // activity under the correct branch regardless of interleaving order.
  const subagentNodesByToolId = new Map<string, TraceNode>();
  const resultNodes: TraceNode[] = [];
  const skillNames = new Set<string>();
  // MCP servers actually used, derived from tool calls named mcp__<server>__<tool>.
  const mcpServers = new Set<string>();
  let coordinatorAgent: string | undefined;

  let activeSubagent: TraceNode | null = null;
  const stats: SessionStats = {
    toolCalls: 0, toolErrors: 0, assistantMessages: 0,
    subagentSpawns: 0, totalTurns: 0, tokensIn: 0, tokensOut: 0,
  };

  function currentParent(): TraceNode {
    return activeSubagent ?? root;
  }

  // Resolve the owning branch for a conversation message. Subagent messages set
  // parent_tool_use_id to the spawning Agent tool id; everything else belongs to
  // the top-level session.
  function resolveMsgParent(msg: Record<string, unknown> | null): TraceNode {
    const pid = msg && typeof msg.parent_tool_use_id === 'string' ? msg.parent_tool_use_id : undefined;
    if (pid) {
      const node = subagentNodesByToolId.get(pid);
      if (node) return node;
    }
    return root;
  }

  for (const event of events) {
    const data = (event.data ?? {}) as Record<string, unknown>;

    if (event.event_type === 'heartbeat') {
      heartbeats.push(event);
      continue;
    }

    if (event.event_type === 'session_start') {
      if (typeof data.agent === 'string' && data.agent.trim()) {
        coordinatorAgent = data.agent.trim();
      }
      // Prefer the full task prompt over the truncated preview so the initial
      // message is shown in full.
      const promptBody = fullPrompt && fullPrompt.trim()
        ? fullPrompt
        : (typeof data.prompt_preview === 'string' ? data.prompt_preview : undefined);
      root.timestamp = event.timestamp;
      root.detail = promptBody;
      root.children.push({
        id: event.id,
        kind: 'lifecycle',
        timestamp: event.timestamp,
        label: 'Session started',
        detail: typeof data.workflow === 'string' ? data.workflow : undefined,
        body: promptBody,
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'session_phase') {
      const phase = typeof data.phase === 'string' ? data.phase : 'unknown';
      if (phase === 'first_sdk_message' || phase === 'claude_query_complete' || phase === 'claude_query_start') {
        continue;
      }
      currentParent().children.push({
        id: event.id,
        kind: 'lifecycle',
        timestamp: event.timestamp,
        label: phase,
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'conversation_batch') {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      for (let i = 0; i < messages.length; i++) {
        const msg = messages[i] as Record<string, unknown> | null;
        if (!msg || typeof msg !== 'object') continue;
        const ts = epochToIso(msg.timestamp, event.timestamp);
        const content = Array.isArray(msg.content) ? msg.content : [];

        /* ── User message ── */
        if (msg.type === 'user') {
          const text = flatText(content);
          if (text) {
            resolveMsgParent(msg).children.push({
              id: `${event.id}-u-${i}`,
              kind: 'user',
              timestamp: ts,
              label: 'User',
              body: text,
              children: [],
              raw: msg,
            });
          }
          continue;
        }

        /* ── Assistant message ── */
        if (msg.type === 'assistant') {
          const text = flatText(content);
          const toolBlocks = content.filter(
            (b: unknown): b is Record<string, unknown> =>
              !!b && typeof b === 'object' && (b as Record<string, unknown>).type === 'tool_use',
          );

          if (text) {
            stats.assistantMessages++;
            resolveMsgParent(msg).children.push({
              id: `${event.id}-a-${i}`,
              kind: 'assistant',
              timestamp: ts,
              label: 'Assistant',
              body: text,
              children: [],
              raw: msg,
            });
          }

          for (const block of toolBlocks) {
            const toolId = typeof block.id === 'string' ? block.id : `${event.id}-${i}-tc`;
            const toolName = typeof block.name === 'string' ? block.name : 'unknown_tool';
            const skillName = toolName === 'Skill' && typeof block.input_preview === 'string'
              ? parseSkillPreview(block.input_preview)
              : undefined;
            const resolvedToolName = toolName === 'Skill' && skillName ? `Skill · ${skillName}` : toolName;
            toolNameMap.set(toolId, resolvedToolName);
            if (skillName) {
              skillNames.add(skillName);
            }
            if (toolName.startsWith('mcp__')) {
              const server = toolName.split('__')[1];
              if (server) mcpServers.add(server);
            }
            stats.toolCalls++;

            const toolNode: TraceNode = {
              id: `${event.id}-tc-${toolId}`,
              kind: 'tool_call',
              timestamp: ts,
              label: resolvedToolName,
              detail: typeof block.input_preview === 'string' ? block.input_preview : undefined,
              children: [],
              raw: block,
              meta: {
                tool_use_id: toolId,
                ...(skillName ? { skill: skillName } : {}),
              },
            };

            if (toolName === 'Agent') {
              const input = typeof block.input_preview === 'string' ? block.input_preview : '';
              const agentInfo = parseAgentPreview(input);

              const subNode: TraceNode = {
                id: `${event.id}-sub-${toolId}`,
                kind: 'subagent',
                timestamp: ts,
                label: agentInfo.name,
                detail: agentInfo.detail,
                children: [],
                raw: block,
                meta: { tool_use_id: toolId },
              };
              stats.subagentSpawns++;
              resolveMsgParent(msg).children.push(subNode);
              activeSubagent = subNode;
              subagentNodesByToolId.set(toolId, subNode);
              pendingToolCalls.set(toolId, subNode);
            } else {
              resolveMsgParent(msg).children.push(toolNode);
              pendingToolCalls.set(toolId, toolNode);
            }
          }
          continue;
        }

        /* ── Tool result ── */
        if (msg.type === 'tool_result') {
          const toolUseId = typeof msg.tool_use_id === 'string' ? msg.tool_use_id : '';
          const preview = typeof msg.content_preview === 'string' ? msg.content_preview : '';
          const isErr = Boolean(msg.is_error);
          if (isErr) stats.toolErrors++;
          const resolvedName = toolNameMap.get(toolUseId) ?? toolUseId;

          const resultNode: TraceNode = {
            id: `${event.id}-tr-${i}`,
            kind: 'tool_result',
            timestamp: ts,
            label: resolvedName,
            body: preview,
            isError: isErr,
            children: [],
            raw: msg,
            meta: { tool_use_id: toolUseId },
          };

          const parentCall = pendingToolCalls.get(toolUseId);
          if (parentCall) {
            parentCall.children.push(resultNode);
            pendingToolCalls.delete(toolUseId);
          } else {
            resolveMsgParent(msg).children.push(resultNode);
          }
          continue;
        }

        /* ── Unknown tool_result via repr fallback ── */
        if (msg.type === 'unknown' && typeof msg.repr === 'string') {
          const toolUseIdMatch = (msg.repr as string).match(/tool_use_id='([^']+)'/);
          if (toolUseIdMatch) {
            const toolUseId = toolUseIdMatch[1];
            const isErr = /is_error=True/.test(msg.repr as string);
            if (isErr) stats.toolErrors++;
            const resolvedName = toolNameMap.get(toolUseId) ?? toolUseId;

            const contentStart = (msg.repr as string).indexOf('content=');
            const contentEnd = (msg.repr as string).lastIndexOf(', is_error=');
            let body = '';
            if (contentStart !== -1) {
              body = contentEnd > contentStart + 8
                ? (msg.repr as string).slice(contentStart + 8, contentEnd)
                : (msg.repr as string).slice(contentStart + 8);
              body = body.replace(/^'/, '').replace(/'\]\)?$/, '').replace(/'$/, '').replace(/\\n/g, '\n').replace(/\\'/g, "'");
            }

            const resultNode: TraceNode = {
              id: `${event.id}-utr-${i}`,
              kind: 'tool_result',
              timestamp: ts,
              label: resolvedName,
              body,
              isError: isErr,
              children: [],
              raw: msg,
              meta: { tool_use_id: toolUseId },
            };

            const parentCall = pendingToolCalls.get(toolUseId);
            if (parentCall) {
              parentCall.children.push(resultNode);
              pendingToolCalls.delete(toolUseId);
            } else {
              resolveMsgParent(msg).children.push(resultNode);
            }
            continue;
          }
        }

        /* ── System / subagent progress ── */
        if (msg.type === 'system') {
          const subtype = typeof msg.subtype === 'string' ? msg.subtype : 'system';
          const sysData = msg.data && typeof msg.data === 'object' ? msg.data as Record<string, unknown> : {};

          if (subtype === 'thinking_tokens') {
            continue;
          }

          if (subtype === 'task_started') {
            const description = typeof sysData.description === 'string' ? sysData.description : 'Subagent task started';
            const taskId = typeof sysData.task_id === 'string' ? sysData.task_id : undefined;
            const toolUseId = typeof sysData.tool_use_id === 'string' ? sysData.tool_use_id : undefined;
            const taskType = summarizeTaskType(sysData.task_type);
            const subagentNode = (toolUseId ? pendingToolCalls.get(toolUseId) : undefined);

            if (subagentNode?.kind === 'subagent') {
              if (subagentNode.label === 'subagent') {
                subagentNode.label = description;
              }
              subagentNode.detail = taskType ?? subagentNode.detail ?? description;
              subagentNode.meta = {
                ...(subagentNode.meta ?? {}),
                ...(taskId ? { task_id: taskId } : {}),
              };
              if (taskId) {
                subagentsByTaskId.set(taskId, subagentNode);
              }

              subagentNode.children.push({
                id: `${event.id}-sts-${i}`,
                kind: 'lifecycle',
                timestamp: ts,
                label: 'task started',
                detail: description,
                children: [],
                raw: msg,
              });
              activeSubagent = subagentNode;
              continue;
            }

            currentParent().children.push({
              id: `${event.id}-sys-${i}`,
              kind: 'lifecycle',
              timestamp: ts,
              label: 'task started',
              detail: description,
              children: [],
              raw: msg,
              meta: taskId ? { task_id: taskId } : undefined,
            });
            continue;
          }

          if (subtype === 'task_progress') {
            // Progress pings duplicate the subagent's real tool activity (now
            // nested via parent_tool_use_id), so they only add noise. Skip them.
            continue;
          }

          const desc = typeof sysData.description === 'string' ? sysData.description
            : typeof sysData.usage === 'string' ? sysData.usage : undefined;
          currentParent().children.push({
            id: `${event.id}-sys-${i}`,
            kind: 'lifecycle',
            timestamp: ts,
            label: subtype,
            detail: desc,
            children: [],
            raw: msg,
          });
          continue;
        }

        /* ── Final result ── */
        if (msg.type === 'result') {
          const preview = typeof msg.result_preview === 'string' ? msg.result_preview : '';
          const turns = typeof msg.num_turns === 'number' ? msg.num_turns : 0;
          stats.totalTurns = turns;
          const previousNode = root.children[root.children.length - 1];
          if (previousNode?.kind === 'assistant' && isDuplicateNarrative(previousNode.body, preview)) {
            root.children.pop();
          }
          const resultNode: TraceNode = {
            id: `${event.id}-res-${i}`,
            kind: 'result',
            timestamp: ts,
            label: 'Result Snapshot',
            body: preview,
            children: [],
            raw: msg,
            meta: turns ? { turns: String(turns) } : undefined,
          };
          resultNodes.push(resultNode);
          root.children.push(resultNode);
        }
      }
      continue;
    }

    /* ── Session complete / error / timeout (top-level events) ── */
    if (event.event_type === 'session_complete') {
      // session_complete carries the untruncated final result. Upgrade the
      // Final Result node (built from the 2k conversation preview) to the full
      // text so nothing is cut off.
      const fullResult = typeof data.result === 'string' ? data.result
        : typeof data.result_preview === 'string' ? data.result_preview : undefined;
      if (fullResult) {
        const resultNode = resultNodes[resultNodes.length - 1] ?? [...root.children].reverse().find((n) => n.kind === 'result');
        if (resultNode) {
          resultNode.label = 'Final Result';
          if ((fullResult.length) > (resultNode.body?.length ?? 0)) resultNode.body = fullResult;
        } else {
          root.children.push({
            id: `${event.id}-res`,
            kind: 'result',
            timestamp: event.timestamp,
            label: 'Final Result',
            body: fullResult,
            children: [],
            raw: data,
          });
        }
      }
      root.children.push({
        id: event.id,
        kind: 'lifecycle',
        timestamp: event.timestamp,
        label: 'session complete',
        detail: 'Final result persisted',
        children: [],
        raw: data,
      });
      if (typeof data.input_tokens === 'number') stats.tokensIn = data.input_tokens as number;
      if (typeof data.output_tokens === 'number') stats.tokensOut = data.output_tokens as number;
      continue;
    }

    if (event.event_type === 'session_error' || event.event_type === 'session_timeout') {
      root.children.push({
        id: event.id,
        kind: 'error',
        timestamp: event.timestamp,
        label: event.event_type === 'session_error' ? 'Session Error' : 'Session Timeout',
        body: typeof data.error === 'string' ? data.error : undefined,
        isError: true,
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'approval_requested' || event.event_type === 'permission_callback' || event.event_type === 'user_question_requested') {
      currentParent().children.push({
        id: event.id,
        kind: 'hook',
        timestamp: event.timestamp,
        label: event.event_type.replace(/_/g, ' '),
        detail: typeof data.tool_name === 'string' ? data.tool_name : typeof data.prompt_preview === 'string' ? data.prompt_preview : undefined,
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'hook_event') {
      const hookName = typeof data.hook_name === 'string' ? data.hook_name : 'hook';
      const hookStatus = typeof data.status === 'string' ? data.status : 'unknown';
      const hookEvent = typeof data.hook_event === 'string' ? data.hook_event : undefined;
      const hookDetail = typeof data.detail === 'string' ? data.detail : undefined;

      currentParent().children.push({
        id: event.id,
        kind: 'hook',
        timestamp: event.timestamp,
        label: `${hookName} · ${hookStatus}`,
        detail: hookEvent,
        body: hookDetail,
        children: [],
        raw: data,
      });
    }
  }

  return {
    root,
    stats,
    heartbeats,
    skillsUsed: Array.from(skillNames).sort(),
    mcpsUsed: Array.from(mcpServers).sort(),
    coordinatorAgent,
  };
}

/* ═══════════════════════════════════════════════════════════════════════════
   Visual configuration per node kind
   ═══════════════════════════════════════════════════════════════════════════ */

const KIND_CONFIG: Record<NodeKind, { icon: string; accent: string; bg: string; border: string; badge?: string }> = {
  session:           { icon: '◉', accent: 'text-[var(--color-info)]',    bg: 'bg-[var(--color-info)]/8',      border: 'border-[var(--color-info)]/20' },
  user:              { icon: '◌', accent: 'text-[var(--color-accent)]',  bg: 'bg-[var(--color-accent)]/8',    border: 'border-[var(--color-accent)]/20',   badge: 'USER' },
  assistant:         { icon: '●', accent: 'text-[var(--color-text-primary)]', bg: 'bg-ops-surface-raised',  border: 'border-ops-border',               badge: 'AI' },
  tool_call:         { icon: '▸', accent: 'text-[var(--color-warning)]', bg: 'bg-[var(--color-warning)]/8',   border: 'border-[var(--color-warning)]/20',  badge: 'CALL' },
  tool_result:       { icon: '◂', accent: 'text-[var(--color-success)]', bg: 'bg-[var(--color-success)]/8',   border: 'border-[var(--color-success)]/20',  badge: 'RESULT' },
  subagent:          { icon: '◈', accent: 'text-[#A78BCA]',             bg: 'bg-[#A78BCA]/8',                border: 'border-[#A78BCA]/20',               badge: 'AGENT' },
  subagent_progress: { icon: '⋯', accent: 'text-[#A78BCA]/70',         bg: 'bg-[#A78BCA]/5',                border: 'border-[#A78BCA]/15' },
  hook:              { icon: '⤷', accent: 'text-[#C08A8A]',             bg: 'bg-[#C08A8A]/8',                border: 'border-[#C08A8A]/20',               badge: 'HOOK' },
  lifecycle:         { icon: '○', accent: 'text-[var(--color-text-tertiary)]', bg: 'bg-ops-surface',        border: 'border-ops-border-subtle' },
  result:            { icon: '✦', accent: 'text-[var(--color-success)]', bg: 'bg-[var(--color-success)]/8',   border: 'border-[var(--color-success)]/20',  badge: 'RESULT' },
  error:             { icon: '✕', accent: 'text-[var(--color-error)]',   bg: 'bg-[var(--color-error)]/10',    border: 'border-[var(--color-error)]/25',    badge: 'ERROR' },
};

function getNodeConfig(node: TraceNode) {
  const base = KIND_CONFIG[node.kind];
  if (node.isError) return { ...base, accent: 'text-[var(--color-error)]', bg: 'bg-[var(--color-error)]/10', border: 'border-[var(--color-error)]/25' };
  return base;
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tree node component — recursive, collapsible
   ═══════════════════════════════════════════════════════════════════════════ */

function TraceNodeView({ node, depth = 0, forceOpen }: { node: TraceNode; depth?: number; forceOpen?: boolean | null }) {
  const hasChildren = node.children.length > 0;
  const defaultState = () => {
    if (depth === 0) return true;
    if (node.kind === 'subagent' || node.kind === 'error' || node.kind === 'result') return true;
    if (node.kind === 'tool_call' && node.children.length > 0) return false;
    return depth < 2;
  };

  const [open, setOpen] = useState(defaultState);
  const [showBody, setShowBody] = useState(false);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    if (forceOpen === true) setOpen(true);
    else if (forceOpen === false) setOpen(false);
  }, [forceOpen]);

  const cfg = getNodeConfig(node);

  return (
    <div className={depth === 0 ? '' : 'relative'}>
      {depth > 0 && (
        <div className="absolute left-[11px] top-0 bottom-0 w-px bg-gradient-to-b from-ops-border to-transparent" style={{ zIndex: 0 }} />
      )}
      <div className={`relative ${depth > 0 ? 'ml-6' : ''}`}>
        {depth > 0 && <div className="absolute -left-[13px] top-[14px] w-3 h-px bg-ops-border" />}

        {/* Node row */}
        <div
          className={`group flex items-start gap-2 rounded-btn px-3 py-2 transition-all duration-150
            ${hasChildren || node.body ? 'cursor-pointer hover:bg-ops-surface-raised/50' : ''} ${cfg.bg} border ${cfg.border}`}
          onClick={() => { if (hasChildren) setOpen(!open); else if (node.body) setShowBody(!showBody); }}
        >
          <span className={`mt-0.5 flex-none text-[10px] w-3 text-center select-none ${cfg.accent}`}>
            {hasChildren ? (open ? '▾' : '▸') : cfg.icon}
          </span>
          {cfg.badge && (
            <span className={`flex-none mt-px rounded px-1.5 py-0.5 text-[9px] font-bold tracking-[0.15em] uppercase
              ${node.isError ? 'bg-[var(--color-error)]/20 text-[var(--color-error)]' : `bg-ops-surface-raised ${cfg.accent}`}`}>{cfg.badge}</span>
          )}
          <span className={`font-medium text-sm leading-tight ${node.isError ? 'text-[var(--color-error)]' : 'text-[var(--color-text-primary)]'}`}>{node.label}</span>
          {node.detail && (
            <span className="flex-1 truncate text-xs text-[var(--color-text-tertiary)] mt-0.5" title={node.detail}>
              {node.detail.length > 80 ? node.detail.slice(0, 80) + '…' : node.detail}
            </span>
          )}
          {node.meta && (
            <div className="hidden group-hover:flex items-center gap-1">
              {Object.entries(node.meta).map(([k, v]) => (
                <span key={k} className="rounded bg-ops-surface-raised px-1.5 py-0.5 text-[9px] text-[var(--color-text-tertiary)] font-mono">
                  {k}: {v.length > 16 ? v.slice(0, 8) + '…' : v}
                </span>
              ))}
            </div>
          )}
          <span className="flex-none text-[10px] text-[var(--color-text-tertiary)] tabular-nums mt-0.5 ml-auto pl-2">{fmtTime(node.timestamp)}</span>
          {Boolean(node.raw) && (
            <button
              className="flex-none text-[9px] text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)] transition-colors mt-0.5"
              onClick={(e) => { e.stopPropagation(); setShowRaw(!showRaw); }}
              title="Toggle raw data"
            >{'{ }'}</button>
          )}
        </div>

        {/* Body */}
        {node.body && (open || showBody || node.kind === 'result' || node.kind === 'error') && (
          <div className={`ml-5 mt-1 mb-2 rounded-btn border ${cfg.border} bg-ops-bg overflow-hidden`}>
            <pre className="p-3 text-[13px] leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap break-words font-[inherit] max-h-[400px] overflow-auto">
              {node.body}
            </pre>
          </div>
        )}

        {/* Raw JSON */}
        {showRaw && Boolean(node.raw) && (
          <div className="ml-5 mt-1 mb-2 rounded-btn border border-ops-border-subtle bg-ops-bg overflow-hidden">
            <pre className="p-3 text-[11px] leading-relaxed text-[var(--color-text-tertiary)] whitespace-pre-wrap break-words max-h-[300px] overflow-auto font-mono">
              {JSON.stringify(node.raw, null, 2)}
            </pre>
          </div>
        )}

        {/* Children */}
        {open && hasChildren && (
          <div className="mt-1 space-y-1">
            {node.children.map((child) => (
              <TraceNodeView key={child.id} node={child} depth={depth + 1} forceOpen={forceOpen} />
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

function StatPill({ label, value, accent }: { label: string; value: string | number; accent?: string }) {
  return (
    <div className="flex items-baseline gap-2 rounded-card border border-ops-border bg-ops-surface px-4 py-3">
      <span className={`text-2xl font-medium tabular-nums ${accent ?? 'text-[var(--color-text-primary)]'}`}>{value}</span>
      <span className="text-[10px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">{label}</span>
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

function TokenStat({ total, input, output, estimated }: { total: number | string; input?: string; output?: string; estimated?: boolean }) {
  const hasTooltip = Boolean((input && output) || estimated);
  const totalDisplay = formatCompactTokenTotal(total);

  return (
    <div className={`group relative ${hasTooltip ? 'cursor-help' : ''}`}>
      <div className="flex items-baseline gap-2 rounded-card border border-ops-border bg-ops-surface px-4 py-3">
        <span className="text-2xl font-medium tabular-nums text-[var(--color-accent)]">{totalDisplay}</span>
        <span className="text-[10px] uppercase tracking-[0.2em] text-[var(--color-text-tertiary)]">Tokens</span>
      </div>

      {hasTooltip && (
        <div className="pointer-events-none absolute left-0 top-full z-20 mt-2 w-max min-w-[180px] rounded-btn border border-ops-border bg-ops-surface-raised px-3 py-2 opacity-0 shadow-card-hover transition-opacity duration-150 group-hover:opacity-100">
          {input && output ? (
            <>
              <div className="flex items-baseline gap-2 text-xs">
                <span className="font-semibold uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">IN:</span>
                <span className="font-mono text-[var(--color-text-primary)]">{input}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-2 text-xs">
                <span className="font-semibold uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">OUT:</span>
                <span className="font-mono text-[var(--color-text-primary)]">{output}</span>
              </div>
            </>
          ) : (
            <div className="text-xs font-medium uppercase tracking-[0.14em] text-[var(--color-text-tertiary)]">
              Estimated total only
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-btn border border-ops-border bg-ops-surface px-3 py-2">
      <p className="text-[9px] uppercase tracking-[0.15em] text-[var(--color-text-tertiary)]">{label}</p>
      <p className="text-lg font-medium text-[var(--color-text-primary)] tabular-nums">{value}</p>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Raw event row
   ═══════════════════════════════════════════════════════════════════════════ */

function RawEventRow({ event }: { event: SessionEvent }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-btn border border-ops-border bg-ops-surface overflow-hidden">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-ops-surface-raised transition-colors">
        <span className="font-mono text-[10px] text-[var(--color-text-tertiary)] tabular-nums w-20 flex-none">{fmtTime(event.timestamp)}</span>
        <span className="text-xs font-medium text-[var(--color-text-secondary)]">{event.event_type}</span>
        <span className="ml-auto text-[10px] text-[var(--color-text-tertiary)]">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-ops-border px-3 pb-3 pt-2">
          <pre className="max-h-80 overflow-auto text-[10px] text-[var(--color-text-tertiary)] whitespace-pre-wrap break-words font-mono">
            {JSON.stringify(event.data, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Page component
   ═══════════════════════════════════════════════════════════════════════════ */

export default function SessionDetailPage() {
  const params = useParams();
  const taskIdParam = params?.taskId;
  const taskId = Array.isArray(taskIdParam) ? taskIdParam[0] : (taskIdParam ?? '');
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [taskRecord, setTaskRecord] = useState<Task | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [viewMode, setViewMode] = useState<'tree' | 'raw'>('tree');
  const [rerunning, setRerunning] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId) { setLoading(false); return; }
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
          setEvents([]);
          return;
        }

        try {
          const detail = await apiFetch<SessionDetail>(`/api/sessions/${taskId}`);
          if (cancelled) return;
          setSession(detail);
          setEvents(detail.events || []);
        } catch {
          // Keep the last known events so heartbeat dots don't disappear on
          // a transient fetch failure after the task finishes.
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

  const promptForTree = taskRecord?.prompt ?? session?.task?.prompt;
  const { root, stats, heartbeats, skillsUsed, mcpsUsed, coordinatorAgent } = useMemo(
    () => buildTree(events, promptForTree),
    [events, promptForTree],
  );
  const [forceOpen, setForceOpen] = useState<boolean | null>(null);

  if (loading) return <p className="text-[var(--color-text-tertiary)] py-20 text-center">Loading session…</p>;
  if (!taskId) return <p className="text-[var(--color-text-tertiary)] py-20 text-center">Invalid session route</p>;
  if (!taskRecord && !session) return <p className="text-[var(--color-text-tertiary)] py-20 text-center">Task not found</p>;
  const task = session?.task ?? taskRecord;
  if (!task) return <p className="text-[var(--color-text-tertiary)] py-20 text-center">Task details not available</p>;

  const rerunTask = async () => {
    if (!RERUNNABLE_STATUSES.has(task.status)) return;
    setActionError(null);
    setRerunning(true);
    try {
      await apiFetch<TaskResetResult>(`/api/tasks/${task.id}/rerun`, { method: 'POST' });
      window.location.href = '/tasks';
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Failed to rerun task');
      setRerunning(false);
    }
  };

  const deleteTask = async () => {
    if (!window.confirm(`Delete task ${task.id.slice(0, 8)} and its related session data?`)) return;
    setActionError(null);
    setDeleting(true);
    try {
      await apiFetch<TaskDeleteResult>(`/api/tasks/${task.id}`, { method: 'DELETE' });
      window.location.href = '/tasks';
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Failed to delete task');
      setDeleting(false);
    }
  };

  const statusStyles: Record<string, string> = {
    queued:    'text-[var(--color-info)] bg-[var(--color-info)]/10 border-[var(--color-info)]/20',
    running:   'text-[var(--color-warning)] bg-[var(--color-warning)]/10 border-[var(--color-warning)]/20',
    succeeded: 'text-[var(--color-success)] bg-[var(--color-success)]/10 border-[var(--color-success)]/20',
    failed:    'text-[var(--color-error)] bg-[var(--color-error)]/10 border-[var(--color-error)]/20',
    lost:      'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border',
    timed_out: 'text-[var(--color-warning)] bg-[var(--color-warning)]/10 border-[var(--color-warning)]/20',
  };

  if (!session && task.status === 'queued') {
    const origin = deriveOrigin(task);
    const metadataEntries = Object.entries(task.metadata ?? {});

    return (
      <div className="space-y-5">
        <header className="rounded-card border border-ops-border bg-ops-surface p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <div className="flex items-center gap-3">
                <h1 className="font-display text-2xl font-normal tracking-tight text-[var(--color-text-primary)]">Queued Task</h1>
                <span className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.1em] ${statusStyles[task.status] ?? 'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border'}`}>
                  {task.status}
                </span>
              </div>
              <p className="text-sm text-[var(--color-text-secondary)]">{task.workflow}</p>
              <p className="font-mono text-[10px] text-[var(--color-text-tertiary)]">{task.id}</p>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 pt-1 text-xs text-[var(--color-text-tertiary)]">
                <span className="inline-flex items-center gap-2">
                  <span className="uppercase tracking-[0.15em]">Origin</span>
                  <span className="text-sm text-[var(--color-text-secondary)]">{origin.icon} {origin.label}</span>
                </span>
                <span className="inline-flex items-center gap-2">
                  <span className="uppercase tracking-[0.15em]">Created</span>
                  <span className="text-sm text-[var(--color-text-secondary)]">{fmt(task.created)}</span>
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
                <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="rounded border-ops-border bg-transparent accent-[var(--color-accent)]" />
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
              <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Request</h2>
              <span className="text-xs text-[var(--color-text-tertiary)]">Waiting for session start</span>
            </div>
            <div className="rounded-btn border border-ops-border-subtle bg-ops-bg overflow-hidden">
              <pre className="p-4 text-[13px] leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap break-words font-[inherit] max-h-[520px] overflow-auto">
                {task.prompt || 'No prompt stored for this task.'}
              </pre>
            </div>
          </section>

          <aside className="space-y-4">
            <section className="rounded-card border border-ops-border bg-ops-surface p-4">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Task Metadata</h3>
              <p className="mb-3 text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">
                This task is still in queue, so there is no session trace yet. The page shows the queued work item and its routing metadata until execution begins.
              </p>
              {metadataEntries.length > 0 ? (
                <div className="space-y-2">
                  {metadataEntries.map(([key, value]) => (
                    <div key={key} className="rounded-btn border border-ops-border-subtle bg-ops-bg px-3 py-2">
                      <p className="text-[9px] uppercase tracking-[0.15em] text-[var(--color-text-tertiary)]">{key}</p>
                      <p className="mt-1 text-xs leading-relaxed text-[var(--color-text-secondary)] break-words">{formatJsonValue(value)}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[var(--color-text-tertiary)]">No metadata captured for this task.</p>
              )}
            </section>
          </aside>
        </div>
      </div>
    );
  }

  if (!session) {
    return <p className="text-[var(--color-text-tertiary)] py-20 text-center">Session has not started yet.</p>;
  }

  const sessionDetail = session;

  const finalTokenTotal = sessionDetail.tokens_input + sessionDetail.tokens_output;
  const totalTokens = finalTokenTotal || task.tokens_used;
  const hasFinalTokenTotals = finalTokenTotal > 0;
  const hasEstimatedTokens = !hasFinalTokenTotals && task.tokens_used > 0;
  const inputTokensDisplay = hasFinalTokenTotals ? sessionDetail.tokens_input.toLocaleString() : '-';
  const outputTokensDisplay = hasFinalTokenTotals ? sessionDetail.tokens_output.toLocaleString() : '-';
  const totalTokensDisplay = totalTokens > 0
    ? totalTokens
    : (task.status === 'lost' || sessionDetail.status === 'running' ? 'n/a' : '0');
  const avgHeartbeatGap = heartbeats.length > 1
    ? Math.round(
        heartbeats.slice(1).reduce((sum, hb, idx) => {
          const cur = new Date(hb.timestamp).getTime();
          const prev = new Date(heartbeats[idx].timestamp).getTime();
          return sum + (cur - prev) / 1000;
        }, 0) / (heartbeats.length - 1),
      )
    : null;

  return (
    <div className="space-y-5">
      {/* ── Header ── */}
      <header className="rounded-card border border-ops-border bg-ops-surface p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-3">
              <h1 className="font-display text-2xl font-normal tracking-tight text-[var(--color-text-primary)]">Session Trace</h1>
              <span className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.1em] ${statusStyles[task.status] ?? 'text-[var(--color-text-tertiary)] bg-ops-surface border-ops-border'}`}>
                {task.status}
              </span>
              {task.status === 'running' && <span className="inline-flex h-2 w-2 rounded-full bg-[var(--color-warning)] animate-pulse" />}
            </div>
            <p className="text-sm text-[var(--color-text-secondary)]">
              {task.workflow}
              {coordinatorAgent && (
                <>
                  <span className="mx-2 text-[var(--color-text-tertiary)]">·</span>
                  <span className="text-[#A78BCA]">agent: {coordinatorAgent}</span>
                </>
              )}
            </p>
            <p className="font-mono text-[10px] text-[var(--color-text-tertiary)]">{taskId}</p>
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
              <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="rounded border-ops-border bg-transparent accent-[var(--color-accent)]" />
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
          <StatPill label="Tool Calls" value={stats.toolCalls} accent="text-[var(--color-warning)]" />
          <StatPill label="Errors" value={stats.toolErrors} accent={stats.toolErrors > 0 ? 'text-[var(--color-error)]' : undefined} />
          <StatPill label="Messages" value={stats.assistantMessages} accent="text-[var(--color-info)]" />
          <StatPill label="Subagents" value={stats.subagentSpawns} accent="text-[#A78BCA]" />
          <StatPill label="Heartbeats" value={heartbeats.length} />
          <TokenStat total={totalTokensDisplay} input={hasFinalTokenTotals ? inputTokensDisplay : undefined} output={hasFinalTokenTotals ? outputTokensDisplay : undefined} estimated={hasEstimatedTokens} />
        </div>
      </header>

      {/* ── Main ── */}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
        <section className="rounded-card border border-ops-border bg-ops-surface p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Trace</h2>
              <span className="text-xs text-[var(--color-text-tertiary)]">{root.children.length} top-level</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex rounded-btn border border-ops-border bg-ops-surface text-[10px]">
                <button onClick={() => setForceOpen(true)}  className="px-2 py-1 text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] transition-colors">Expand all</button>
                <button onClick={() => setForceOpen(false)} className="px-2 py-1 text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] transition-colors border-l border-ops-border">Collapse</button>
                <button onClick={() => setForceOpen(null)}  className="px-2 py-1 text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] transition-colors border-l border-ops-border">Auto</button>
              </div>
              <div className="flex rounded-btn border border-ops-border bg-ops-surface p-0.5 text-xs">
                {(['tree', 'raw'] as const).map((m) => (
                  <button key={m} onClick={() => setViewMode(m)}
                    className={`rounded-[8px] px-2.5 py-1 transition-all ${viewMode === m ? 'bg-ops-surface-raised text-[var(--color-text-primary)] font-medium' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]'}`}>
                    {m === 'tree' ? 'Tree' : 'Raw'}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {viewMode === 'tree' ? (
            <div className="space-y-1">
              {root.children.length === 0
                ? <p className="py-16 text-center text-[var(--color-text-tertiary)]">No trace entries yet</p>
                : root.children.map((child) => <TraceNodeView key={child.id} node={child} depth={0} forceOpen={forceOpen} />)}
            </div>
          ) : (
            <div className="space-y-1.5">
              {events.map((event) => <RawEventRow key={event.id} event={event} />)}
            </div>
          )}
        </section>

        {/* Sidebar */}
        <aside className="space-y-4">
          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Runtime</h3>
            <div className="grid grid-cols-2 gap-2">
              <MiniStat label="Duration" value={fmtDur(sessionDetail.duration_sec ?? task.duration_sec)} />
              <MiniStat label="Turns" value={sessionDetail.turns || stats.totalTurns || '-'} />
              <MiniStat label="HB Gap" value={avgHeartbeatGap != null ? `${avgHeartbeatGap}s` : '-'} />
              <MiniStat label="Events" value={events.length} />
            </div>
            {hasEstimatedTokens && (
              <p className="mt-3 text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">
                Showing the last known token count from `task_progress` events. Final totals were not written because the session did not reach `session_complete`.
              </p>
            )}
            {!hasEstimatedTokens && totalTokens === 0 && (task.status === 'lost' || sessionDetail.status === 'running') && (
              <p className="mt-3 text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">
                Final token totals are only persisted on `session_complete`. This task ended without a final result, so token totals are unavailable from current telemetry.
              </p>
            )}
          </section>

          {sessionDetail.subagents_used && sessionDetail.subagents_used.length > 0 && (
            <section className="rounded-card border border-ops-border bg-ops-surface p-4">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Subagents</h3>
              <div className="space-y-2">
                {sessionDetail.subagents_used.map((sa) => (
                  <div key={sa.name} className="flex items-center justify-between rounded-btn border border-[#A78BCA]/15 bg-[#A78BCA]/5 px-3 py-2">
                    <span className="text-xs font-medium text-[#A78BCA]">{sa.name}</span>
                    <span className="text-[10px] text-[var(--color-text-tertiary)]">{sa.turns}t · {sa.tokens}tok</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Skills</h3>
            {skillsUsed.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {skillsUsed.map((skill) => (
                  <span key={skill} className="rounded-full border border-[var(--color-accent)]/20 bg-[var(--color-accent-muted)] px-3 py-1 text-xs text-[var(--color-accent)]">
                    {skill}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-[var(--color-text-tertiary)]">No explicit Skill tool invocation was recorded in this session.</p>
            )}
          </section>

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">MCPs</h3>
            {mcpsUsed.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {mcpsUsed.map((mcp) => (
                  <span key={mcp} className="rounded-full border border-[var(--color-info)]/20 bg-[var(--color-info)]/8 px-3 py-1 text-xs text-[var(--color-info)]">
                    {mcp}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-[var(--color-text-tertiary)]">No MCP tool was invoked in this session.</p>
            )}
          </section>

          {sessionDetail.tools_used && sessionDetail.tools_used.length > 0 && (
            <section className="rounded-card border border-ops-border bg-ops-surface p-4">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Top Tools</h3>
              <div className="space-y-1.5">
                {sessionDetail.tools_used.slice(0, 8).map((t) => (
                  <div key={t.name} className="flex items-center justify-between text-xs">
                    <span className="text-[var(--color-text-secondary)] truncate max-w-[160px]" title={t.name}>{t.name}</span>
                    <span className="text-[var(--color-text-tertiary)] tabular-nums">{t.count}×</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-[var(--color-text-secondary)]">Heartbeats</h3>
              <span className="text-[10px] text-[var(--color-text-tertiary)]">{heartbeats.length}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {heartbeats.slice(-60).map((hb, idx, arr) => (
                <span
                  key={hb.id}
                  className={`h-1.5 w-1.5 rounded-full ${
                    TERMINAL_STATUSES.has(task.status)
                      ? 'bg-ops-border'
                      : idx === arr.length - 1
                        ? 'bg-[var(--color-success)]'
                        : 'bg-ops-border'
                  }`}
                  title={fmt(hb.timestamp)}
                />
              ))}
              {heartbeats.length === 0 && <p className="text-[10px] text-[var(--color-text-tertiary)]">None recorded</p>}
            </div>
          </section>

          <section className="rounded-card border border-ops-border bg-ops-surface p-4">
            <h3 className="text-sm font-medium text-[var(--color-text-secondary)] mb-3">Task Metadata</h3>
            <pre className="text-[10px] text-[var(--color-text-tertiary)] whitespace-pre-wrap break-words max-h-48 overflow-auto font-mono leading-relaxed">
              {JSON.stringify(task.metadata, null, 2)}
            </pre>
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
    amber: 'border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 text-[var(--color-warning)] hover:bg-[var(--color-warning)]/15 hover:text-[var(--color-text-primary)]',
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
