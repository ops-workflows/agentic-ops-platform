'use client';

import { useEffect, useState } from 'react';
import {
  apiFetch,
  type AgentMemoryDetail,
  type HindsightBankDetail,
  type HindsightBank,
  type HindsightGraphPreview,
  type PlatformMemories,
} from '@/lib/api';

type VisualGraphNodeKind = 'document' | 'chunk' | 'keyword' | 'memory';

type VisualGraphNode = {
  id: string;
  label: string;
  kind: VisualGraphNodeKind;
  radius: number;
};

type VisualGraphEdge = {
  source: string;
  target: string;
  weight: number;
};

type Selection =
  | { kind: 'hindsight'; id: string }
  | { kind: 'agent'; id: string };

const GRAPH_STOP_WORDS = new Set([
  'about',
  'after',
  'agent',
  'analysis',
  'assistant',
  'bank',
  'case',
  'chunk',
  'content',
  'context',
  'created',
  'data',
  'date',
  'detail',
  'document',
  'event',
  'false',
  'field',
  'from',
  'hook',
  'incident',
  'learning',
  'memory',
  'message',
  'metadata',
  'null',
  'prompt',
  'result',
  'session',
  'status',
  'task',
  'text',
  'timestamp',
  'tool',
  'trace',
  'true',
  'type',
  'updated',
  'workflow',
  'world',
]);

function formatDateTime(value: string | null) {
  if (!value) return '-';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatNumber(value: number) {
  return value.toLocaleString();
}

function stringifyValue(value: unknown) {
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object' && value !== null) return JSON.stringify(value);
  if (value === null || value === undefined || value === '') return '-';
  return String(value);
}

function shortenLabel(value: string, limit = 42) {
  const compact = value.replace(/\s+/g, ' ').trim();
  if (compact.length <= limit) return compact;
  return `${compact.slice(0, limit - 1).trimEnd()}…`;
}

function summarizeChunkRow(row: Record<string, unknown>) {
  const text = String(row.text ?? '')
    .replace(/\\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (text) return shortenLabel(text.replace(/^['"[{(\s]+/, ''), 48);
  const context = String(row.context ?? '')
    .replace(/\s+/g, ' ')
    .trim();
  if (context) return shortenLabel(context, 48);
  return shortenLabel(String(row.chunk_id ?? row.id ?? 'Chunk'), 48);
}

function shortDocumentLabel(documentId: string) {
  if (documentId.startsWith('task-')) {
    return `Task ${documentId.slice(-8)}`;
  }
  return shortenLabel(documentId, 20);
}

function extractGraphKeywords(text: string) {
  const matches = text.match(/[A-Za-z][A-Za-z0-9_:-]{3,}/g) ?? [];
  const scores = new Map<string, number>();

  for (const match of matches) {
    const trimmed = match.replace(/^[_:-]+|[_:-]+$/g, '');
    if (!trimmed) continue;
    const lower = trimmed.toLowerCase();
    if (GRAPH_STOP_WORDS.has(lower)) continue;
    if (/^node-?\d+$/i.test(trimmed)) continue;
    if (/^\d+$/.test(trimmed)) continue;

    let score = trimmed.length;
    if (trimmed.includes('__')) score += 20;
    if (/[A-Z]/.test(trimmed.slice(1))) score += 12;
    if (/_/.test(trimmed)) score += 8;
    if (
      /error|flow|rule|salesforce|validation|close|reclamation|investigator/i.test(
        trimmed,
      )
    )
      score += 10;
    scores.set(trimmed, Math.max(scores.get(trimmed) ?? 0, score));
  }

  return Array.from(scores.entries())
    .sort(
      (left, right) => right[1] - left[1] || left[0].localeCompare(right[0]),
    )
    .slice(0, 3)
    .map(([keyword]) => keyword);
}

function hashString(value: string) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

function isGenericGraph(graph: HindsightGraphPreview) {
  return graph.nodes.every(
    (node, index) =>
      node.label === `Node ${index + 1}` || /^node-\d+$/i.test(node.id),
  );
}

function buildVisualGraph(graph: HindsightGraphPreview | null) {
  const documentNodes = new Map<string, VisualGraphNode>();
  const chunkNodes = new Map<string, VisualGraphNode>();
  const keywordNodes = new Map<string, VisualGraphNode>();
  const edgeWeights = new Map<string, number>();

  if (
    graph &&
    graph.nodes.length > 0 &&
    graph.edges.length > 0 &&
    !isGenericGraph(graph)
  ) {
    const nodes = graph.nodes.slice(0, 28).map((node) => ({
      id: node.id,
      label: shortenLabel(node.label, 28),
      kind: (node.node_type === 'document'
        ? 'document'
        : node.node_type === 'keyword'
          ? 'keyword'
          : 'memory') as VisualGraphNodeKind,
      radius:
        node.node_type === 'document'
          ? 12
          : node.node_type === 'keyword'
            ? 7
            : 9,
    }));
    const edges = graph.edges
      .filter(
        (edge) =>
          nodes.some((node) => node.id === edge.source) &&
          nodes.some((node) => node.id === edge.target),
      )
      .slice(0, 60)
      .map((edge) => ({
        source: edge.source,
        target: edge.target,
        weight: edge.weight ?? 1,
      }));
    return { nodes, edges, mode: 'live' as const };
  }

  for (const row of graph?.table_rows.slice(0, 22) ?? []) {
    const chunkId = String(
      row.chunk_id ?? row.id ?? `chunk-${chunkNodes.size + 1}`,
    );
    const documentId =
      typeof row.document_id === 'string' ? row.document_id : null;
    if (!chunkNodes.has(chunkId)) {
      chunkNodes.set(chunkId, {
        id: chunkId,
        label: summarizeChunkRow(row),
        kind: 'chunk',
        radius: 7,
      });
    }

    if (documentId && !documentNodes.has(documentId)) {
      documentNodes.set(documentId, {
        id: documentId,
        label: shortDocumentLabel(documentId),
        kind: 'document',
        radius: 12,
      });
    }

    if (documentId) {
      const documentEdgeKey = `${documentId}::${chunkId}`;
      edgeWeights.set(
        documentEdgeKey,
        (edgeWeights.get(documentEdgeKey) ?? 0) + 1,
      );
    }

    const keywordSource = `${String(row.text ?? '')} ${String(row.context ?? '')}`;
    for (const keyword of extractGraphKeywords(keywordSource)) {
      const keywordId = `keyword:${keyword.toLowerCase()}`;
      if (!keywordNodes.has(keywordId)) {
        keywordNodes.set(keywordId, {
          id: keywordId,
          label: shortenLabel(keyword.replace(/^mcp__/, ''), 20),
          kind: 'keyword',
          radius: 6,
        });
      }
      const keywordEdgeKey = `${chunkId}::${keywordId}`;
      edgeWeights.set(
        keywordEdgeKey,
        (edgeWeights.get(keywordEdgeKey) ?? 0) + 1,
      );
    }
  }

  const nodes = [
    ...Array.from(documentNodes.values()),
    ...Array.from(chunkNodes.values()),
    ...Array.from(keywordNodes.values()),
  ].slice(0, 36);
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  const edges = Array.from(edgeWeights.entries())
    .map(([key, weight]) => {
      const [source, target] = key.split('::');
      return { source, target, weight };
    })
    .filter(
      (edge) =>
        visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target),
    )
    .slice(0, 72);

  return { nodes, edges, mode: 'derived' as const };
}

function layoutVisualGraph(
  nodes: VisualGraphNode[],
  edges: VisualGraphEdge[],
  width: number,
  height: number,
) {
  const positions = new Map(
    nodes.map((node) => {
      const seed = hashString(node.id);
      const x = 80 + (seed % Math.max(1, width - 160));
      const y = 70 + ((seed >> 8) % Math.max(1, height - 140));
      return [node.id, { x, y, vx: 0, vy: 0 }];
    }),
  );

  for (let iteration = 0; iteration < 140; iteration += 1) {
    for (let index = 0; index < nodes.length; index += 1) {
      const left = nodes[index];
      const leftPosition = positions.get(left.id);
      if (!leftPosition) continue;

      for (let inner = index + 1; inner < nodes.length; inner += 1) {
        const right = nodes[inner];
        const rightPosition = positions.get(right.id);
        if (!rightPosition) continue;

        const dx = rightPosition.x - leftPosition.x;
        const dy = rightPosition.y - leftPosition.y;
        const distanceSquared = Math.max(dx * dx + dy * dy, 0.01);
        const distance = Math.sqrt(distanceSquared);
        const repulsion = 2200 / distanceSquared;
        const offsetX = (dx / distance) * repulsion;
        const offsetY = (dy / distance) * repulsion;

        leftPosition.vx -= offsetX;
        leftPosition.vy -= offsetY;
        rightPosition.vx += offsetX;
        rightPosition.vy += offsetY;
      }
    }

    for (const edge of edges) {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01);
      const idealLength = 70;
      const pull = (distance - idealLength) * 0.01 * Math.min(edge.weight, 3);
      const offsetX = (dx / distance) * pull;
      const offsetY = (dy / distance) * pull;

      source.vx += offsetX;
      source.vy += offsetY;
      target.vx -= offsetX;
      target.vy -= offsetY;
    }

    for (const node of nodes) {
      const position = positions.get(node.id);
      if (!position) continue;
      const centerPullX = (width / 2 - position.x) * 0.003;
      const centerPullY = (height / 2 - position.y) * 0.003;
      position.vx = (position.vx + centerPullX) * 0.78;
      position.vy = (position.vy + centerPullY) * 0.78;
      position.x = clamp(position.x + position.vx, 28, width - 28);
      position.y = clamp(position.y + position.vy, 28, height - 28);
    }
  }

  return positions;
}

export default function MemoryPage() {
  const [summary, setSummary] = useState<PlatformMemories | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [bankDetail, setBankDetail] = useState<HindsightBankDetail | null>(
    null,
  );
  const [agentDetail, setAgentDetail] = useState<AgentMemoryDetail | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<PlatformMemories>('/api/platform/memories')
      .then((payload) => {
        setSummary(payload);
        const search = new URLSearchParams(window.location.search);
        const requestedBank = search.get('bank');
        const requestedAgent = search.get('agent');
        if (
          requestedBank &&
          payload.hindsight_banks.some((bank) => bank.bank_id === requestedBank)
        ) {
          setSelection({ kind: 'hindsight', id: requestedBank });
          return;
        }
        if (
          requestedAgent &&
          payload.agent_memories.some(
            (agent) => agent.agent_name === requestedAgent,
          )
        ) {
          setSelection({ kind: 'agent', id: requestedAgent });
          return;
        }

        const firstBank = payload.hindsight_banks[0]?.bank_id;
        const firstAgent = payload.agent_memories[0]?.agent_name;
        if (firstBank) {
          setSelection({ kind: 'hindsight', id: firstBank });
        } else if (firstAgent) {
          setSelection({ kind: 'agent', id: firstAgent });
        }
      })
      .catch((err) =>
        setError(
          err instanceof Error ? err.message : 'Failed to load memory summary',
        ),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selection) return;
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.delete('bank');
    nextUrl.searchParams.delete('agent');
    if (selection.kind === 'hindsight') {
      nextUrl.searchParams.set('bank', selection.id);
    } else {
      nextUrl.searchParams.set('agent', selection.id);
    }
    window.history.replaceState({}, '', nextUrl);
  }, [selection]);

  useEffect(() => {
    if (!selection) {
      setBankDetail(null);
      setAgentDetail(null);
      return;
    }

    setDetailLoading(true);

    if (selection.kind === 'hindsight') {
      setAgentDetail(null);
      apiFetch<HindsightBankDetail>(
        `/api/platform/memories/hindsight/${encodeURIComponent(selection.id)}?limit=10`,
      )
        .then(setBankDetail)
        .catch(() =>
          setBankDetail({
            bank_id: selection.id,
            listed_in_hindsight: false,
            warnings: [
              'Failed to load bank detail from the control-plane API.',
            ],
            stats: {
              total_nodes: 0,
              total_links: 0,
              total_documents: 0,
              total_observations: 0,
              pending_operations: 0,
              failed_operations: 0,
              nodes_by_fact_type: {},
              links_by_link_type: {},
            },
            graph: null,
            entries: [],
          }),
        )
        .finally(() => setDetailLoading(false));
      return;
    }

    setBankDetail(null);
    apiFetch<AgentMemoryDetail>(
      `/api/platform/memories/agents/${encodeURIComponent(selection.id)}`,
    )
      .then(setAgentDetail)
      .catch(() =>
        setAgentDetail({
          agent_name: selection.id,
          archive_key: null,
          files: [],
        }),
      )
      .finally(() => setDetailLoading(false));
  }, [selection]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-4xl font-normal leading-[1.1] tracking-tight text-[var(--color-text-primary)]">
          Memory explorer
        </h1>
        <p className="mt-3 max-w-3xl text-sm leading-7 text-[var(--color-text-secondary)]">
          Inspect Hindsight bank contents and Claude agent memory files from one
          place.
        </p>
      </div>

      {loading ? (
        <p className="text-sm text-[var(--color-text-tertiary)]">
          Loading memory surfaces...
        </p>
      ) : null}
      {error ? (
        <p className="text-sm text-[var(--color-error)]">{error}</p>
      ) : null}

      {!loading && !error && summary ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(220px,280px)_minmax(0,1fr)]">
          <aside className="rounded-card border border-ops-border bg-ops-surface p-3 xl:sticky xl:top-24 xl:self-start">
            <HindsightSection
              banks={summary.hindsight_banks}
              selection={selection}
              onSelect={(bankId) =>
                setSelection({ kind: 'hindsight', id: bankId })
              }
            />

            <div className="my-3 h-px bg-ops-border" />

            <MemorySection
              title="Claude Agent Memory Files"
              items={summary.agent_memories.map((agent) => ({
                id: agent.agent_name,
                label: agent.agent_name,
                meta: `${agent.version_count} archives`,
                active:
                  selection?.kind === 'agent' &&
                  selection.id === agent.agent_name,
                onClick: () =>
                  setSelection({ kind: 'agent', id: agent.agent_name }),
              }))}
              emptyMessage="No agent memory archives discovered"
            />
          </aside>

          <section className="min-w-0 rounded-card border border-ops-border bg-ops-surface p-5">
            {detailLoading ? (
              <p className="text-sm text-[var(--color-text-tertiary)]">
                Loading selected memory...
              </p>
            ) : null}

            {!detailLoading && selection?.kind === 'hindsight' && bankDetail ? (
              <div className="space-y-4">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-xl font-medium text-[var(--color-text-primary)]">
                      {bankDetail.bank_id}
                    </h2>
                  </div>
                  <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                    Live Hindsight bank inspection focused on retained memory
                    units and graph structure.
                  </p>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <MemoryTag
                      label={
                        bankDetail.listed_in_hindsight
                          ? 'Listed live'
                          : 'Configured only'
                      }
                    />
                    {summary.hindsight_banks.find(
                      (bank) => bank.bank_id === bankDetail.bank_id,
                    )?.kind ? (
                      <MemoryTag
                        label={
                          summary.hindsight_banks.find(
                            (bank) => bank.bank_id === bankDetail.bank_id,
                          )?.kind || 'unknown'
                        }
                      />
                    ) : null}
                    {(
                      summary.hindsight_banks.find(
                        (bank) => bank.bank_id === bankDetail.bank_id,
                      )?.workflows || []
                    ).length === 0 ? (
                      <MemoryTag label="Shared default" />
                    ) : (
                      (
                        summary.hindsight_banks.find(
                          (bank) => bank.bank_id === bankDetail.bank_id,
                        )?.workflows || []
                      ).map((workflow) => (
                        <MemoryTag key={workflow} label={workflow} />
                      ))
                    )}
                  </div>
                  <div className="mt-2 text-xs text-[var(--color-text-tertiary)]">
                    {bankDetail.entries.length} memory units
                  </div>
                </div>

                {bankDetail.warnings.length > 0 ? (
                  <div className="space-y-2 rounded-[16px] border border-[var(--color-accent)]/20 bg-[var(--color-accent-muted)] px-4 py-3">
                    <div className="text-xs uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
                      Operator notes
                    </div>
                    {bankDetail.warnings.map((warning) => (
                      <p
                        key={warning}
                        className="text-sm leading-6 text-[var(--color-text-secondary)]"
                      >
                        {warning}
                      </p>
                    ))}
                  </div>
                ) : null}

                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-3">
                  <StatCard
                    label="Nodes"
                    value={formatNumber(bankDetail.stats.total_nodes)}
                  />
                  <StatCard
                    label="Links"
                    value={formatNumber(bankDetail.stats.total_links)}
                  />
                  <StatCard
                    label="Documents"
                    value={formatNumber(bankDetail.stats.total_documents)}
                  />
                </div>

                <section className="rounded-[16px] border border-ops-border bg-[var(--color-surface-raised)] p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-medium text-[var(--color-text-primary)]">
                        Knowledge graph
                      </h3>
                      <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
                        Uses Hindsight graph data when it is meaningful,
                        otherwise derives a relationship map from chunk rows and
                        recurring terms.
                      </p>
                    </div>
                    <div className="text-xs text-[var(--color-text-tertiary)]">
                      {bankDetail.graph
                        ? `${bankDetail.graph.nodes.length} nodes / ${bankDetail.graph.edges.length} edges shown`
                        : 'Unavailable'}
                    </div>
                  </div>
                  <div className="mt-4">
                    <BankGraphPreview graph={bankDetail.graph} />
                  </div>
                </section>

                <DetailSection
                  title="Memory units"
                  subtitle="Rows from /memories/list for this bank."
                >
                  {bankDetail.entries.map((entry) => (
                    <article
                      key={entry.id}
                      className="rounded-[12px] border border-ops-border bg-[var(--color-surface-raised)] p-4"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="text-xs text-[var(--color-text-tertiary)]">
                          {entry.id}
                        </div>
                        <div className="text-xs text-[var(--color-text-tertiary)]">
                          {formatDateTime(entry.created_at)}
                        </div>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {Object.entries(entry.metadata)
                          .slice(0, 6)
                          .map(([key, value]) => (
                            <MemoryTag
                              key={`${entry.id}-${key}`}
                              label={`${key}: ${stringifyValue(value)}`}
                            />
                          ))}
                      </div>
                      <pre className="mt-3 max-h-[260px] overflow-auto whitespace-pre-wrap break-words rounded-[10px] bg-ops-surface p-3 text-xs leading-6 text-[var(--color-text-secondary)]">
                        {entry.content}
                      </pre>
                    </article>
                  ))}
                  {bankDetail.entries.length === 0 ? (
                    <EmptyState message="No memory units returned for this bank." />
                  ) : null}
                </DetailSection>
              </div>
            ) : null}

            {!detailLoading && selection?.kind === 'agent' && agentDetail ? (
              <div className="space-y-4">
                <div>
                  <h2 className="text-xl font-medium text-[var(--color-text-primary)]">
                    {agentDetail.agent_name}
                  </h2>
                  <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                    Readable file previews from the latest Claude agent memory
                    archive.
                  </p>
                  <div className="mt-2 text-xs text-[var(--color-text-tertiary)]">
                    {agentDetail.archive_key || '-'}
                  </div>
                </div>

                {agentDetail.files.map((file) => (
                  <article
                    key={file.path}
                    className="rounded-[12px] border border-ops-border bg-[var(--color-surface-raised)] p-4"
                  >
                    <div className="text-xs font-medium text-[var(--color-text-primary)]">
                      {file.path}
                    </div>
                    <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                      {file.size_bytes.toLocaleString()} bytes
                    </div>
                    <pre className="mt-3 max-h-[260px] overflow-auto whitespace-pre-wrap break-words rounded-[10px] bg-ops-surface p-3 text-xs leading-6 text-[var(--color-text-secondary)]">
                      {file.preview}
                    </pre>
                  </article>
                ))}

                {agentDetail.files.length === 0 ? (
                  <p className="text-sm text-[var(--color-text-tertiary)]">
                    No readable files found in this archive.
                  </p>
                ) : null}
              </div>
            ) : null}

            {!detailLoading && !selection ? (
              <p className="text-sm text-[var(--color-text-tertiary)]">
                Select a memory source from the left.
              </p>
            ) : null}
          </section>
        </div>
      ) : null}
    </div>
  );
}

function MemorySection({
  title,
  items,
  emptyMessage,
}: {
  title: string;
  items: Array<{
    id: string;
    label: string;
    meta: string;
    active: boolean;
    onClick: () => void;
  }>;
  emptyMessage: string;
}) {
  return (
    <div className="space-y-2">
      <div className="px-2 text-[11px] uppercase tracking-[0.18em] text-[var(--color-text-tertiary)]">
        {title}
      </div>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={item.onClick}
          className={`w-full rounded-[12px] border px-3 py-3 text-left transition-colors ${
            item.active
              ? 'border-[var(--color-accent)]/30 bg-[var(--color-accent-muted)]'
              : 'border-transparent bg-[var(--color-surface-raised)] hover:border-ops-border'
          }`}
        >
          <div className="text-sm font-medium text-[var(--color-text-primary)]">
            {item.label}
          </div>
          <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">
            {item.meta}
          </div>
        </button>
      ))}
      {items.length === 0 ? (
        <div className="px-2 py-3 text-sm text-[var(--color-text-tertiary)]">
          {emptyMessage}
        </div>
      ) : null}
    </div>
  );
}

function HindsightSection({
  banks,
  selection,
  onSelect,
}: {
  banks: HindsightBank[];
  selection: Selection | null;
  onSelect: (bankId: string) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="px-2 text-[11px] uppercase tracking-[0.18em] text-[var(--color-text-tertiary)]">
        Hindsight Banks
      </div>
      {banks.map((bank) => {
        const active =
          selection?.kind === 'hindsight' && selection.id === bank.bank_id;
        return (
          <button
            key={bank.bank_id}
            type="button"
            onClick={() => onSelect(bank.bank_id)}
            className={`w-full rounded-[12px] border px-3 py-3 text-left transition-colors ${
              active
                ? 'border-[var(--color-accent)]/30 bg-[var(--color-accent-muted)]'
                : 'border-transparent bg-[var(--color-surface-raised)] hover:border-ops-border'
            }`}
          >
            <div className="text-sm font-medium text-[var(--color-text-primary)]">
              {bank.bank_id}
            </div>
          </button>
        );
      })}
      {banks.length === 0 ? (
        <div className="px-2 py-3 text-sm text-[var(--color-text-tertiary)]">
          No Hindsight banks discovered
        </div>
      ) : null}
    </div>
  );
}

function MemoryTag({ label }: { label: string }) {
  return (
    <span className="rounded-full bg-ops-surface px-2.5 py-1 text-[10px] text-[var(--color-text-tertiary)]">
      {label}
    </span>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[14px] border border-ops-border bg-[var(--color-surface-raised)] px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className="mt-2 text-lg font-medium text-[var(--color-text-primary)]">
        {value}
      </div>
    </div>
  );
}

function DetailSection({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <section className="min-w-0 space-y-3 rounded-[16px] border border-ops-border bg-[var(--color-surface-raised)] p-4">
      <div>
        <h3 className="text-sm font-medium text-[var(--color-text-primary)]">
          {title}
        </h3>
        <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
          {subtitle}
        </p>
      </div>
      {children}
    </section>
  );
}

function EmptyState({ message }: { message: string }) {
  return <p className="text-sm text-[var(--color-text-tertiary)]">{message}</p>;
}

function BankGraphPreview({ graph }: { graph: HindsightGraphPreview | null }) {
  if (!graph || graph.nodes.length === 0) {
    return <EmptyState message="No graph nodes returned for this bank." />;
  }

  const visualGraph = buildVisualGraph(graph);
  if (visualGraph.nodes.length === 0) {
    return (
      <EmptyState message="No useful graph relationships could be derived for this bank." />
    );
  }

  const width = 760;
  const height = 460;
  const positions = layoutVisualGraph(
    visualGraph.nodes,
    visualGraph.edges,
    width,
    height,
  );
  const legend = [
    { label: 'Documents', kind: 'document' as const },
    { label: 'Chunks', kind: 'chunk' as const },
    { label: 'Recurring terms', kind: 'keyword' as const },
  ];

  const colorByKind: Record<
    VisualGraphNodeKind,
    { fill: string; stroke: string; label: string }
  > = {
    document: { fill: '#f4b860', stroke: '#f8d59a', label: '#fff6e9' },
    chunk: { fill: '#6aa6ff', stroke: '#bbd5ff', label: '#dceaff' },
    keyword: { fill: '#8fe3c0', stroke: '#d8fff0', label: '#eafff5' },
    memory: { fill: '#c5b3ff', stroke: '#e4dcff', label: '#f3efff' },
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_260px]">
      <div className="overflow-hidden rounded-[20px] border border-ops-border bg-[#0f1720] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <svg viewBox={`0 0 ${width} ${height}`} className="h-[460px] w-full">
          <defs>
            <radialGradient id="graphGlow" cx="50%" cy="50%" r="70%">
              <stop offset="0%" stopColor="#182534" />
              <stop offset="100%" stopColor="#0a1018" />
            </radialGradient>
            <pattern
              id="graphGrid"
              width="32"
              height="32"
              patternUnits="userSpaceOnUse"
            >
              <path
                d="M 32 0 L 0 0 0 32"
                fill="none"
                stroke="rgba(190,210,255,0.08)"
                strokeWidth="1"
              />
            </pattern>
          </defs>
          <rect width={width} height={height} fill="url(#graphGlow)" />
          <rect
            width={width}
            height={height}
            fill="url(#graphGrid)"
            opacity="0.6"
          />
          {visualGraph.edges.map((edge) => {
            const source = positions.get(edge.source);
            const target = positions.get(edge.target);
            if (!source || !target) return null;
            return (
              <line
                key={`${edge.source}-${edge.target}`}
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
                stroke="rgba(154, 194, 255, 0.22)"
                strokeWidth={Math.max(1, Math.min(edge.weight, 3))}
                opacity="0.9"
              />
            );
          })}
          {visualGraph.nodes.map((node) => {
            const position = positions.get(node.id);
            if (!position) return null;
            const palette = colorByKind[node.kind];
            return (
              <g key={node.id}>
                <circle
                  cx={position.x}
                  cy={position.y}
                  r={node.radius + 10}
                  fill={palette.fill}
                  opacity="0.12"
                />
                <circle
                  cx={position.x}
                  cy={position.y}
                  r={node.radius}
                  fill={palette.fill}
                  stroke={palette.stroke}
                  strokeWidth="1.5"
                />
                <title>{node.label}</title>
                {node.kind !== 'chunk' ? (
                  <text
                    x={position.x + node.radius + 8}
                    y={position.y + 4}
                    fill={palette.label}
                    fontSize="12"
                  >
                    {shortenLabel(node.label, 22)}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>

      <div className="space-y-3">
        <div className="rounded-[16px] border border-ops-border bg-ops-surface px-4 py-3">
          <div className="text-xs uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
            Legend
          </div>
          <div className="mt-3 space-y-2">
            {legend.map((item) => (
              <div
                key={item.kind}
                className="flex items-center gap-3 text-sm text-[var(--color-text-secondary)]"
              >
                <span
                  className="h-3 w-3 rounded-full"
                  style={{ backgroundColor: colorByKind[item.kind].fill }}
                />
                <span>{item.label}</span>
              </div>
            ))}
          </div>
        </div>

        {graph.table_rows.length > 0 ? (
          <div className="space-y-2">
            <div className="text-xs uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
              Source rows
            </div>
            {graph.table_rows.slice(0, 4).map((row, index) => (
              <article
                key={`${index}-${stringifyValue(row.id)}`}
                className="rounded-[12px] border border-ops-border bg-ops-surface px-3 py-3"
              >
                <div className="text-sm font-medium text-[var(--color-text-primary)]">
                  {summarizeChunkRow(row)}
                </div>
                <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                  {stringifyValue(
                    row.context ?? row.entities ?? row.date ?? '-',
                  )}
                </div>
              </article>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
