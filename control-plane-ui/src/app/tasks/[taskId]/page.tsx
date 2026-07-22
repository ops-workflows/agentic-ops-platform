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
import { Highlight, themes } from 'prism-react-renderer';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  apiFetch,
  type SessionDetail,
  type SessionEvent,
  type Task,
  type TaskDeleteResult,
  type TaskResetResult,
} from '@/lib/api';
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
  | 'thinking'
  | 'messaging'
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
  badge?: string;
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
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
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

function formatTraceBody(input: string): string {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return input;
  }
}

function getJsonBody(input: string): string | undefined {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return undefined;
  }
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
    name =
      typeof parsed.subagent_type === 'string'
        ? parsed.subagent_type
        : typeof parsed.description === 'string'
          ? parsed.description
          : name;
    detail =
      typeof parsed.description === 'string' ? parsed.description : undefined;
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

function parseSendMessagePreview(input: string): {
  recipientId?: string;
  message?: string;
  summary?: string;
} {
  try {
    const parsed = JSON.parse(input) as Record<string, unknown>;
    return {
      recipientId:
        typeof parsed.to === 'string'
          ? parsed.to
          : typeof parsed.recipient === 'string'
            ? parsed.recipient
            : undefined,
      message:
        typeof parsed.message === 'string'
          ? parsed.message
          : typeof parsed.content === 'string'
            ? parsed.content
            : undefined,
      summary: typeof parsed.summary === 'string' ? parsed.summary : undefined,
    };
  } catch {
    return {
      recipientId: input.match(/"(?:to|recipient)"\s*:\s*"([^"]+)"/)?.[1],
      message: input.match(/"(?:message|content)"\s*:\s*"([^"]+)"/)?.[1],
      summary: input.match(/"summary"\s*:\s*"([^"]+)"/)?.[1],
    };
  }
}

function parseSendMessageResult(input: string): {
  message?: string;
  recipientId?: string;
} {
  const nestedJsonMatch = input.match(
    /["']text["']\s*:\s*["'](\{.*\})["']\s*}/,
  );
  const normalized = nestedJsonMatch?.[1]?.replace(/\\"/g, '"') ?? input;
  try {
    const parsed = JSON.parse(normalized) as Record<string, unknown>;
    return {
      message: typeof parsed.message === 'string' ? parsed.message : undefined,
      recipientId:
        typeof parsed.recipient === 'string'
          ? parsed.recipient
          : typeof parsed.to === 'string'
            ? parsed.to
            : undefined,
    };
  } catch {
    const messageMatch = input.match(
      /"message"\s*:\s*"([\s\S]*?)"\s*,\s*"(?:resumedAgentId|pin|recipient|success)"/,
    );
    if (messageMatch) {
      let message = messageMatch[1];
      for (let index = 0; index < 2; index++) {
        message = message.replace(/\\"/g, '"');
      }
      return {
        message: message.replace(/\\'/g, "'").replace(/\\n/g, '\n'),
        recipientId: input.match(
          /(?:delivery to|recipient[":\s]+)([\w-]+)/i,
        )?.[1],
      };
    }
    return {
      message: input
        .match(/"message"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/)?.[1]
        ?.replace(/\\"/g, '"'),
      recipientId: input.match(
        /(?:delivery to|recipient[":\s]+)([\w-]+)/i,
      )?.[1],
    };
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
  const prefixLength = Math.min(
    normalizedLeft.length,
    normalizedRight.length,
    280,
  );
  return (
    normalizedLeft.slice(0, prefixLength) ===
    normalizedRight.slice(0, prefixLength)
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tree builder — converts flat events into hierarchical trace
   ═══════════════════════════════════════════════════════════════════════════ */

function buildTree(
  events: SessionEvent[],
  fullPrompt?: string,
  includeThinking = false,
): {
  root: TraceNode;
  stats: SessionStats;
  heartbeats: SessionEvent[];
  skillsUsed: string[];
  mcpsUsed: string[];
} {
  const root: TraceNode = {
    id: 'root',
    kind: 'session',
    timestamp: events[0]?.timestamp ?? new Date().toISOString(),
    label: 'Trace',
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
  const subagentNamesByAgentId = new Map<string, string>();
  const resultNodes: TraceNode[] = [];
  const skillNames = new Set<string>();
  // MCP servers actually used, derived from tool calls named mcp__<server>__<tool>.
  const mcpServers = new Set<string>();
  let defaultAgent: string | undefined;

  let activeSubagent: TraceNode | null = null;
  const stats: SessionStats = {
    toolCalls: 0,
    toolErrors: 0,
    assistantMessages: 0,
    subagentSpawns: 0,
    totalTurns: 0,
    tokensIn: 0,
    tokensOut: 0,
  };

  function currentParent(): TraceNode {
    return activeSubagent ?? root;
  }

  function appendSubagentResult(parent: TraceNode, result: TraceNode) {
    parent.children.push({
      ...result,
      kind: 'result',
      label: '',
      meta: { ...(result.meta ?? {}), result_role: 'subagent_return' },
    });
  }

  // Resolve the owning branch for a conversation message. Subagent messages set
  // parent_tool_use_id to the spawning Agent tool id; everything else belongs to
  // the top-level session.
  function resolveMsgParent(msg: Record<string, unknown> | null): TraceNode {
    const pid =
      msg && typeof msg.parent_tool_use_id === 'string'
        ? msg.parent_tool_use_id
        : undefined;
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
        defaultAgent = data.agent.trim();
        root.label = defaultAgent;
      }
      // Prefer the full task prompt over the truncated preview so the initial
      // message is shown in full.
      const promptBody = fullPrompt?.trim()
        ? fullPrompt
        : typeof data.prompt_preview === 'string'
          ? data.prompt_preview
          : undefined;
      root.timestamp = event.timestamp;
      root.detail = promptBody;
      root.children.push({
        id: event.id,
        kind: 'lifecycle',
        timestamp: event.timestamp,
        label: '',
        detail: '',
        body: promptBody,
        badge: 'REQUEST',
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'session_phase') {
      const phase = typeof data.phase === 'string' ? data.phase : 'unknown';
      if (
        phase === 'first_sdk_message' ||
        phase === 'claude_query_complete' ||
        phase === 'claude_query_start'
      ) {
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
          const thinking = content
            .filter(
              (b: unknown): b is Record<string, unknown> =>
                !!b &&
                typeof b === 'object' &&
                (b as Record<string, unknown>).type === 'thinking' &&
                typeof (b as Record<string, unknown>).preview === 'string',
            )
            .map((b) => b.preview as string)
            .join('\n\n')
            .trim();
          const toolBlocks = content.filter(
            (b: unknown): b is Record<string, unknown> =>
              !!b &&
              typeof b === 'object' &&
              (b as Record<string, unknown>).type === 'tool_use',
          );

          if (text) {
            stats.assistantMessages++;
            resolveMsgParent(msg).children.push({
              id: `${event.id}-a-${i}`,
              kind: 'assistant',
              timestamp: ts,
              label: '',
              body: text,
              children: [],
              raw: msg,
            });
          }

          if (includeThinking && thinking) {
            resolveMsgParent(msg).children.push({
              id: `${event.id}-think-${i}`,
              kind: 'thinking',
              timestamp: ts,
              label: 'Reasoning',
              body: thinking,
              children: [],
              raw: msg,
            });
          }

          for (const block of toolBlocks) {
            const toolId =
              typeof block.id === 'string' ? block.id : `${event.id}-${i}-tc`;
            const toolName =
              typeof block.name === 'string' ? block.name : 'unknown_tool';
            const skillName =
              toolName === 'Skill' && typeof block.input_preview === 'string'
                ? parseSkillPreview(block.input_preview)
                : undefined;
            const resolvedToolName =
              toolName === 'Skill' && skillName ? skillName : toolName;
            const isSkill = toolName === 'Skill' && Boolean(skillName);
            const mcpMatch = toolName.match(/^mcp__([^_]+)__(.+)$/);
            const isMcp = Boolean(mcpMatch);
            const traceToolName = mcpMatch
              ? `${mcpMatch[1]} · ${mcpMatch[2]}`
              : resolvedToolName;
            const toolInput =
              !isSkill && typeof block.input_preview === 'string'
                ? formatTraceBody(block.input_preview)
                : undefined;
            toolNameMap.set(toolId, resolvedToolName);
            if (skillName) {
              skillNames.add(skillName);
            }
            if (toolName.startsWith('mcp__')) {
              const server = toolName.split('__')[1];
              if (server) mcpServers.add(server);
            }
            stats.toolCalls++;

            const isInternalMessage = toolName === 'SendMessage';
            const messageInfo =
              isInternalMessage && typeof block.input_preview === 'string'
                ? parseSendMessagePreview(block.input_preview)
                : undefined;
            const parent = resolveMsgParent(msg);
            const sender =
              parent === root ? (defaultAgent ?? 'Coordinator') : parent.label;
            const toolNode: TraceNode = {
              id: `${event.id}-tc-${toolId}`,
              kind: isInternalMessage ? 'messaging' : 'tool_call',
              timestamp: ts,
              label: isInternalMessage
                ? `${sender} -> ${messageInfo?.recipientId ?? 'specialist'}`
                : traceToolName,
              body: isInternalMessage
                ? messageInfo?.message
                : isSkill
                  ? undefined
                  : toolInput,
              badge: isSkill ? 'SKILL' : isMcp ? 'MCP' : undefined,
              children: [],
              raw: block,
              meta: {
                tool_use_id: toolId,
                ...(isInternalMessage && messageInfo?.recipientId
                  ? { recipient_id: messageInfo.recipientId }
                  : {}),
                ...(skillName ? { skill: skillName } : {}),
              },
            };

            if (toolName === 'Agent') {
              const input =
                typeof block.input_preview === 'string'
                  ? block.input_preview
                  : '';
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
          const toolUseId =
            typeof msg.tool_use_id === 'string' ? msg.tool_use_id : '';
          const preview =
            typeof msg.content_preview === 'string' ? msg.content_preview : '';
          const isErr = Boolean(msg.is_error);
          if (isErr) stats.toolErrors++;
          const resultNode: TraceNode = {
            id: `${event.id}-tr-${i}`,
            kind: 'tool_result',
            timestamp: ts,
            label: '',
            body: formatTraceBody(preview),
            isError: isErr,
            children: [],
            raw: msg,
            meta: { tool_use_id: toolUseId },
          };

          const parentCall = pendingToolCalls.get(toolUseId);
          if (parentCall) {
            if (parentCall.kind === 'messaging') {
              const delivery = parseSendMessageResult(preview);
              parentCall.children.push({
                ...resultNode,
                kind: 'result',
                label: '',
                body: formatTraceBody(delivery.message ?? preview),
              });
              pendingToolCalls.delete(toolUseId);
              continue;
            }
            if (parentCall.kind === 'subagent') {
              const agentId = preview.match(/agentId:\s*['"]?([\w-]+)/)?.[1];
              if (agentId) {
                subagentNamesByAgentId.set(agentId, parentCall.label);
                parentCall.meta = {
                  ...(parentCall.meta ?? {}),
                  agent_id: agentId,
                };
              }
              appendSubagentResult(parentCall, resultNode);
              pendingToolCalls.delete(toolUseId);
              continue;
            }
            parentCall.children.push(resultNode);
            pendingToolCalls.delete(toolUseId);
          } else {
            resolveMsgParent(msg).children.push(resultNode);
          }
          continue;
        }

        /* ── Unknown tool_result via repr fallback ── */
        if (msg.type === 'unknown' && typeof msg.repr === 'string') {
          const toolUseIdMatch = (msg.repr as string).match(
            /tool_use_id='([^']+)'/,
          );
          if (toolUseIdMatch) {
            const toolUseId = toolUseIdMatch[1];
            const isErr = /is_error=True/.test(msg.repr as string);
            if (isErr) stats.toolErrors++;
            const contentStart = (msg.repr as string).indexOf('content=');
            const contentEnd = (msg.repr as string).lastIndexOf(', is_error=');
            let body = '';
            if (contentStart !== -1) {
              body =
                contentEnd > contentStart + 8
                  ? (msg.repr as string).slice(contentStart + 8, contentEnd)
                  : (msg.repr as string).slice(contentStart + 8);
              body = body
                .replace(/^'/, '')
                .replace(/'\]\)?$/, '')
                .replace(/'$/, '')
                .replace(/\\n/g, '\n')
                .replace(/\\'/g, "'");
            }

            const resultNode: TraceNode = {
              id: `${event.id}-utr-${i}`,
              kind: 'tool_result',
              timestamp: ts,
              label: '',
              body: formatTraceBody(body),
              isError: isErr,
              children: [],
              raw: msg,
              meta: { tool_use_id: toolUseId },
            };

            const parentCall = pendingToolCalls.get(toolUseId);
            if (parentCall) {
              if (parentCall.kind === 'subagent') {
                appendSubagentResult(parentCall, resultNode);
                pendingToolCalls.delete(toolUseId);
                continue;
              }
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
          const subtype =
            typeof msg.subtype === 'string' ? msg.subtype : 'system';
          const sysData =
            msg.data && typeof msg.data === 'object'
              ? (msg.data as Record<string, unknown>)
              : {};

          if (
            subtype === 'thinking_tokens' ||
            subtype === 'init' ||
            subtype === 'task_updated' ||
            subtype === 'task_notification'
          ) {
            continue;
          }

          if (subtype === 'task_started') {
            const description =
              typeof sysData.description === 'string'
                ? sysData.description
                : 'Subagent task started';
            const taskId =
              typeof sysData.task_id === 'string' ? sysData.task_id : undefined;
            const toolUseId =
              typeof sysData.tool_use_id === 'string'
                ? sysData.tool_use_id
                : undefined;
            const taskType = summarizeTaskType(sysData.task_type);
            const subagentNode = toolUseId
              ? pendingToolCalls.get(toolUseId)
              : undefined;

            if (subagentNode?.kind === 'subagent') {
              if (subagentNode.label === 'subagent') {
                subagentNode.label = description;
              }
              subagentNode.detail =
                taskType ?? subagentNode.detail ?? description;
              subagentNode.meta = {
                ...(subagentNode.meta ?? {}),
                ...(taskId ? { task_id: taskId } : {}),
              };
              if (taskId) {
                subagentsByTaskId.set(taskId, subagentNode);
              }

              activeSubagent = subagentNode;
              continue;
            }

            continue;
          }

          if (subtype === 'task_progress') {
            // Progress pings duplicate the subagent's real tool activity and
            // only add noise to the task trace.
            continue;
          }

          const desc =
            typeof sysData.description === 'string'
              ? sysData.description
              : typeof sysData.usage === 'string'
                ? sysData.usage
                : undefined;
          currentParent().children.push({
            id: `${event.id}-sys-${i}`,
            kind: 'lifecycle',
            timestamp: ts,
            label: subtype === 'init' && defaultAgent ? defaultAgent : subtype,
            detail: subtype === 'init' ? 'init' : desc,
            badge: subtype === 'init' && defaultAgent ? 'AGENT' : undefined,
            children: [],
            raw: msg,
          });
          continue;
        }

        /* ── Final result ── */
        if (msg.type === 'result') {
          const preview =
            typeof msg.result_preview === 'string' ? msg.result_preview : '';
          const turns = typeof msg.num_turns === 'number' ? msg.num_turns : 0;
          stats.totalTurns = turns;
          const previousNode = root.children[root.children.length - 1];
          if (
            previousNode?.kind === 'assistant' &&
            isDuplicateNarrative(previousNode.body, preview)
          ) {
            root.children.pop();
          }
          const resultNode: TraceNode = {
            id: `${event.id}-res-${i}`,
            kind: 'result',
            timestamp: ts,
            label: 'Snapshot',
            body: preview,
            children: [],
            raw: msg,
            meta: {
              result_role: 'session_result',
              ...(turns ? { turns: String(turns) } : {}),
            },
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
      const fullResult =
        typeof data.result === 'string'
          ? data.result
          : typeof data.result_preview === 'string'
            ? data.result_preview
            : undefined;
      if (fullResult) {
        const resultNode =
          resultNodes[resultNodes.length - 1] ??
          [...root.children].reverse().find((n) => n.kind === 'result');
        if (resultNode) {
          resultNode.label = 'Final';
          if (fullResult.length > (resultNode.body?.length ?? 0))
            resultNode.body = fullResult;
        } else {
          root.children.push({
            id: `${event.id}-res`,
            kind: 'result',
            timestamp: event.timestamp,
            label: 'Final',
            body: fullResult,
            children: [],
            raw: data,
            meta: { result_role: 'session_result' },
          });
        }
      }
      if (typeof data.input_tokens === 'number')
        stats.tokensIn = data.input_tokens as number;
      if (typeof data.output_tokens === 'number')
        stats.tokensOut = data.output_tokens as number;
      continue;
    }

    if (
      event.event_type === 'session_error' ||
      event.event_type === 'session_timeout'
    ) {
      root.children.push({
        id: event.id,
        kind: 'error',
        timestamp: event.timestamp,
        label:
          event.event_type === 'session_error'
            ? 'Session Error'
            : 'Session Timeout',
        body: typeof data.error === 'string' ? data.error : undefined,
        isError: true,
        children: [],
        raw: data,
      });
      continue;
    }

    if (
      event.event_type === 'approval_requested' ||
      event.event_type === 'permission_callback' ||
      event.event_type === 'user_question_requested'
    ) {
      currentParent().children.push({
        id: event.id,
        kind: 'hook',
        timestamp: event.timestamp,
        label: event.event_type.replace(/_/g, ' '),
        detail:
          typeof data.tool_name === 'string'
            ? data.tool_name
            : typeof data.prompt_preview === 'string'
              ? data.prompt_preview
              : undefined,
        children: [],
        raw: data,
      });
      continue;
    }

    if (event.event_type === 'hook_event') {
      const hookName =
        typeof data.hook_name === 'string' ? data.hook_name : 'hook';
      const hookStatus =
        typeof data.status === 'string' ? data.status : 'unknown';
      const hookEvent =
        typeof data.hook_event === 'string' ? data.hook_event : undefined;
      const hookDetail =
        typeof data.detail === 'string' ? data.detail : undefined;

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

  function replaceRecipientIds(nodes: TraceNode[]) {
    for (const node of nodes) {
      if (node.kind === 'messaging' && node.meta?.recipient_id) {
        const recipient = subagentNamesByAgentId.get(node.meta.recipient_id);
        if (recipient) {
          node.label = node.label.replace(node.meta.recipient_id, recipient);
          node.meta = { ...node.meta, recipient };
        }
      }
      replaceRecipientIds(node.children);
    }
  }
  replaceRecipientIds(root.children);

  function moveCanonicalResultsToEnd(node: TraceNode) {
    for (const child of node.children) {
      moveCanonicalResultsToEnd(child);
    }
    const canonicalResults = node.children.filter(
      (child) =>
        child.meta?.result_role === 'subagent_return' ||
        (node === root && child.meta?.result_role === 'session_result'),
    );
    if (canonicalResults.length) {
      node.children = [
        ...node.children.filter((child) => !canonicalResults.includes(child)),
        ...canonicalResults,
      ];
    }
  }
  moveCanonicalResultsToEnd(root);

  return {
    root,
    stats,
    heartbeats,
    skillsUsed: Array.from(skillNames).sort(),
    mcpsUsed: Array.from(mcpServers).sort(),
  };
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
    <Highlight code={body} language="json" theme={themes.vsDark}>
      {({ className, getLineProps, getTokenProps, style, tokens }) => (
        <pre
          className={`${className} overflow-auto ${compact ? 'max-h-48 px-0 text-[10px]' : 'max-h-[400px] p-3 text-[13px]'} leading-relaxed`}
          style={{ ...style, background: 'transparent' }}
        >
          {tokens.map((line, lineIndex) => (
            <div
              {...getLineProps({ line })}
              key={`line-${lineIndex}-${line.join('')}`}
            >
              {line.map((token, tokenIndex) => (
                <span
                  {...getTokenProps({ token })}
                  key={`token-${tokenIndex}-${token.content}`}
                />
              ))}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}

function TraceBody({ body }: { body: string }) {
  const json = getJsonBody(body);

  if (json) {
    return <JsonCode body={json} />;
  }

  return (
    <div className="max-h-[400px] overflow-auto px-3 py-2 text-[13px] leading-relaxed text-[var(--color-text-secondary)]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => (
            <a
              className="text-[var(--color-info)] underline underline-offset-2"
              href={href}
              rel="noreferrer"
              target="_blank"
            >
              {children}
            </a>
          ),
          code: ({ children }) => (
            <code className="rounded bg-ops-surface-raised px-1 py-0.5 font-mono text-[12px]">
              {children}
            </code>
          ),
          h1: ({ children }) => (
            <h1 className="mb-2 text-lg font-semibold text-[var(--color-text-primary)]">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-2 text-base font-semibold text-[var(--color-text-primary)]">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-1 text-sm font-semibold text-[var(--color-text-primary)]">
              {children}
            </h3>
          ),
          li: ({ children }) => <li className="ml-5 list-disc">{children}</li>,
          ol: ({ children }) => <ol className="my-2">{children}</ol>,
          p: ({ children }) => (
            <p className="my-2 first:mt-0 last:mb-0">{children}</p>
          ),
          pre: ({ children }) => (
            <pre className="my-2 overflow-auto rounded bg-ops-surface-raised p-2 font-mono text-[12px]">
              {children}
            </pre>
          ),
          ul: ({ children }) => <ul className="my-2">{children}</ul>,
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tree node component — recursive, collapsible
   ═══════════════════════════════════════════════════════════════════════════ */

function TraceNodeView({
  node,
  depth = 0,
  forceOpen,
}: {
  node: TraceNode;
  depth?: number;
  forceOpen?: boolean | null;
}) {
  const hasChildren = node.children.length > 0;
  const hasBody = Boolean(node.body);
  const defaultState = () => {
    return (
      node.badge === 'REQUEST' ||
      (node.kind === 'result' && node.label === 'Final')
    );
  };

  const [open, setOpen] = useState(defaultState);

  useEffect(() => {
    if (forceOpen === true) setOpen(true);
    else if (forceOpen === false) setOpen(false);
  }, [forceOpen]);

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
            {fmtTime(node.timestamp)}
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
            className={`relative mt-1 space-y-1 ${node.children.length > 1 ? 'before:absolute before:top-[14px] before:bottom-[14px] before:left-[11px] before:w-px before:bg-ops-border' : ''}`}
          >
            {node.children.map((child) => (
              <TraceNodeView
                key={child.id}
                node={child}
                depth={depth + 1}
                forceOpen={forceOpen}
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
   Raw event row
   ═══════════════════════════════════════════════════════════════════════════ */

function RawEventRow({ event }: { event: SessionEvent }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-btn border border-ops-border bg-ops-surface overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-ops-surface-raised transition-colors"
      >
        <span className="font-mono text-[10px] text-[var(--color-text-tertiary)] tabular-nums w-20 flex-none">
          {fmtTime(event.timestamp)}
        </span>
        <span className="text-xs font-medium text-[var(--color-text-secondary)]">
          {event.event_type}
        </span>
        <span className="ml-auto text-[10px] text-[var(--color-text-tertiary)]">
          {open ? '▾' : '▸'}
        </span>
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
  const taskId = Array.isArray(taskIdParam)
    ? taskIdParam[0]
    : (taskIdParam ?? '');
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [taskRecord, setTaskRecord] = useState<Task | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [viewMode, setViewMode] = useState<'tree' | 'raw'>('tree');
  const [showThinking, setShowThinking] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

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
          setEvents([]);
          return;
        }

        try {
          const detail = await apiFetch<SessionDetail>(
            `/api/sessions/${taskId}`,
          );
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
  const { root, stats, heartbeats, skillsUsed, mcpsUsed } = useMemo(
    () => buildTree(events, promptForTree, showThinking),
    [events, promptForTree, showThinking],
  );
  const [forceOpen, setForceOpen] = useState<boolean | null>(null);

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

  const finalTokenTotal =
    sessionDetail.tokens_input + sessionDetail.tokens_output;
  const totalTokens = Math.max(finalTokenTotal, task.tokens_used);
  const hasFinalTokenTotals = finalTokenTotal > 0;
  const inputTokensDisplay = hasFinalTokenTotals
    ? sessionDetail.tokens_input.toLocaleString()
    : '-';
  const outputTokensDisplay = hasFinalTokenTotals
    ? sessionDetail.tokens_output.toLocaleString()
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
            const cur = new Date(hb.timestamp).getTime();
            const prev = new Date(heartbeats[idx].timestamp).getTime();
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
              <div className="flex rounded-btn border border-ops-border bg-ops-surface p-0.5 text-xs">
                {(['tree', 'raw'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setViewMode(m)}
                    className={`rounded-[8px] px-2.5 py-1 transition-all ${viewMode === m ? 'bg-ops-surface-raised text-[var(--color-text-primary)] font-medium' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]'}`}
                  >
                    {m === 'tree' ? 'Tree' : 'Raw'}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {viewMode === 'tree' ? (
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
                  />
                ))
              )}
            </div>
          ) : (
            <div className="space-y-1.5">
              {events.map((event) => (
                <RawEventRow key={event.id} event={event} />
              ))}
            </div>
          )}
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
              <MiniStat label="Events" value={events.length} />
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
