const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8080';

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

// Types matching the FastAPI response models

export interface Agent {
  id: string;
  name: string;
  description: string | null;
  version: string | null;
  provisioned: boolean;
  paused: boolean;
  provisioned_at: string | null;
  config: Record<string, unknown>;
  repo_path: string | null;
  created: string;
  updated: string;
}

export interface AgentFilesResult {
  agent_yaml: string;
  files: Record<string, string>;
}

export interface Task {
  id: string;
  workflow: string;
  prompt: string;
  status: string;
  channel: string | null;
  metadata: Record<string, unknown>;
  message_channel: string | null;
  message_thread: string | null;
  tokens_used: number;
  duration_sec: number | null;
  error: string | null;
  wait_reason: string | null;
  wait_deadline: string | null;
  archived_at: string | null;
  created: string;
  updated: string;
}

export interface TaskListResult {
  items: Task[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionDetail {
  id: string;
  task_id: string | null;
  agent_id: string | null;
  status: string;
  started: string | null;
  ended: string | null;
  duration_sec: number | null;
  tokens_input: number;
  tokens_output: number;
  turns: number;
  task: Task | null;
  tools_used: Array<{ name: string; count: number; total_duration: number }>;
  subagents_used: Array<{ name: string; turns: number; tokens: number }>;
  error: string | null;
  events: SessionEvent[];
}

export interface SessionEvent {
  id: string;
  event_type: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface Analytics {
  total_tasks: number;
  succeeded: number;
  failed: number;
  avg_duration_sec: number | null;
  total_tokens: number;
  tasks_by_workflow: Record<string, number>;
  tasks_by_status: Record<string, number>;
  daily_counts: Array<{ date: string; count: number }>;
  tokens_by_workflow?: Record<string, number>;
}

export interface TaskResetResult {
  status: string;
  task: Task;
}

export interface TaskDeleteResult {
  status: string;
  task_id: string;
  workflow: string;
  container_removed: boolean;
}

export interface TaskArchiveResult {
  status: string;
  task: Task;
}

export interface Schedule {
  id: string;
  agent_name: string;
  schedule_name: string;
  cron_expression: string;
  prompt: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  created_at: string;
}

export interface ApprovalItem {
  id: string;
  task_id: string;
  workflow: string | null;
  task_status: string | null;
  approval_kind: string;
  tool_name: string;
  status: string;
  request_preview: string | null;
  reason: string | null;
  resolved_by: string | null;
  resolved_by_user_id: string | null;
  requested_at: string;
  resolved_at: string | null;
  archived_at: string | null;
}

export interface PlatformApprovals {
  counts_by_status: Record<string, number>;
  items: ApprovalItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface McpTool {
  name: string;
  description: string;
  read_only: boolean;
  open_world: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  description: string;
  usage_count: number;
  used_by: string[];
  tools: McpTool[];
}

export interface Connector {
  id: string;
  name: string;
  summary: string;
  description: string | null;
  source_type: string;
  source_label: string;
  target_workflow: string | null;
  target_channel: string | null;
  tags: string[];
  type: string;
}

export interface WorkflowRepoStatus {
  source_url: string | null;
  source_mode: 'remote' | 'local';
  default_ref: string | null;
  pinned_ref: string | null;
  last_synced_ref: string | null;
  last_synced_commit: string | null;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  discovered_workflows: string[];
  bundle_errors: Record<string, string>;
}

export interface WorkflowRepoVersion {
  name: string;
  commit_sha: string | null;
}

export interface HindsightBank {
  bank_id: string;
  label: string;
  kind: string;
  workflows: string[];
  listed_in_hindsight: boolean;
}

export interface AgentMemory {
  agent_name: string;
  latest_key: string | null;
  latest_updated_at: string | null;
  version_count: number;
}

export interface PlatformMemories {
  hindsight_available: boolean;
  hindsight_banks: HindsightBank[];
  agent_memories: AgentMemory[];
}

export interface BackgroundJobRun {
  id: string;
  job_type: string;
  scope: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_sec: number | null;
  summary: Record<string, unknown>;
  warnings: string[];
  error: string | null;
}

export interface PlatformBackgroundJobs {
  items: BackgroundJobRun[];
  total: number;
  limit: number;
  offset: number;
}

export interface HindsightMemoryEntry {
  id: string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface HindsightGraphNode {
  id: string;
  label: string;
  node_type: string;
}

export interface HindsightGraphEdge {
  source: string;
  target: string;
  edge_type: string;
  weight: number | null;
}

export interface HindsightGraphPreview {
  nodes: HindsightGraphNode[];
  edges: HindsightGraphEdge[];
  table_rows: Array<Record<string, unknown>>;
  total_units: number;
}

export interface HindsightBankStats {
  total_nodes: number;
  total_links: number;
  total_documents: number;
  total_observations: number;
  pending_operations: number;
  failed_operations: number;
  nodes_by_fact_type: Record<string, number>;
  links_by_link_type: Record<string, number>;
}

export interface HindsightBankDetail {
  bank_id: string;
  listed_in_hindsight: boolean;
  warnings: string[];
  stats: HindsightBankStats;
  graph: HindsightGraphPreview | null;
  entries: HindsightMemoryEntry[];
}

export interface AgentMemoryFile {
  path: string;
  size_bytes: number;
  preview: string;
}

export interface AgentMemoryDetail {
  agent_name: string;
  archive_key: string | null;
  files: AgentMemoryFile[];
}
