'use client';

import { useParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import {
  apiFetch,
  type BackgroundJobRun,
  type GitHubConnection,
  type KnowledgeSource,
  type KnowledgeSourceVersion,
  type PlatformBackgroundJobs,
} from '@/lib/api';
import {
  KnowledgeSourceForm,
  repositoryName,
  sourcePayload,
} from '@/components/knowledge-source-form';

const BLANK = {
  repository: '',
  default_ref: '',
  include_paths: '',
  exclude_paths: '',
  credential_ref: '',
  schedule: 'daily',
  enabled: true,
};

export default function KnowledgeSourceDetailPage() {
  const params = useParams();
  const rawId = params?.sourceId;
  const sourceId = Array.isArray(rawId) ? rawId[0] : rawId || '';
  const [source, setSource] = useState<KnowledgeSource | null>(null);
  const [form, setForm] = useState(BLANK);
  const [versions, setVersions] = useState<KnowledgeSourceVersion[]>([]);
  const [runs, setRuns] = useState<BackgroundJobRun[]>([]);
  const [connections, setConnections] = useState<GitHubConnection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const currentVersion = versions.find(
    (version) => version.id === source?.current_successful_version_id,
  );

  async function load() {
    if (!sourceId) return;
    try {
      const [item, sourceVersions, jobs, configuredConnections] =
        await Promise.all([
          apiFetch<KnowledgeSource>(
            `/api/platform/knowledge-sources/${sourceId}`,
          ),
          apiFetch<KnowledgeSourceVersion[]>(
            `/api/platform/knowledge-sources/${sourceId}/versions`,
          ),
          apiFetch<PlatformBackgroundJobs>(
            `/api/platform/background-jobs?knowledge_source_id=${sourceId}&limit=20&offset=0`,
          ),
          apiFetch<GitHubConnection[]>(
            '/api/platform/knowledge-sources/github-connections',
          ),
        ]);
      setSource(item);
      setVersions(sourceVersions);
      setRuns(jobs.items);
      setConnections(configuredConnections);
      setForm({
        repository: item.repository_url
          .replace(/^https:\/\/[^/]+\//, '')
          .replace(/\.git$/, ''),
        default_ref: item.default_ref,
        include_paths: item.include_paths.join('\n'),
        exclude_paths: item.exclude_paths.join('\n'),
        credential_ref: item.credential_ref || '',
        schedule:
          item.sync_policy.interval_sec === 3600
            ? 'hourly'
            : item.sync_policy.interval_sec === 604800
              ? 'weekly'
              : item.sync_policy.interval_sec
                ? 'daily'
                : '',
        enabled: item.enabled,
      });
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : 'Failed to load source',
      );
    }
  }
  useEffect(() => {
    load();
  }, [sourceId]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/api/platform/knowledge-sources/${sourceId}`, {
        method: 'PUT',
        body: JSON.stringify(sourcePayload(form)),
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Save failed');
    } finally {
      setBusy(false);
    }
  }
  async function sync() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/api/platform/knowledge-sources/${sourceId}/sync`, {
        method: 'POST',
      });
      await load();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : 'Sync request failed',
      );
    } finally {
      setBusy(false);
    }
  }

  if (!source)
    return (
      <p className="text-sm text-[var(--color-text-tertiary)]">
        {error || 'Loading source...'}
      </p>
    );
  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-4xl font-normal text-[var(--color-text-primary)]">
            {repositoryName(source.repository_url)}
          </h1>
          <p className="mt-2 text-sm text-[var(--color-text-tertiary)]">
            {source.repository_url} · {source.default_ref}
            {currentVersion
              ? ` · Current index: ${currentVersion.commit_sha.slice(0, 12)}`
              : ' · No promoted index yet'}
          </p>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={sync}
          className="inline-flex items-center gap-2 border border-ops-border bg-ops-surface px-4 py-2 text-sm"
        >
          <RefreshCw size={16} />
          Queue sync
        </button>
      </header>
      {error ? (
        <p className="text-sm text-[var(--color-error)]">{error}</p>
      ) : null}
      <section>
        <h2 className="mb-3 text-lg font-medium">Configuration</h2>
        <KnowledgeSourceForm
          form={form}
          setForm={setForm}
          onSubmit={save}
          submitLabel={busy ? 'Saving...' : 'Save changes'}
          connections={connections}
        />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-medium">Sync history</h2>
        <div className="divide-y divide-ops-border border-y border-ops-border">
          {runs.map((run) => {
            const version = versions.find(
              (item) => item.id === run.knowledge_source_version_id,
            );
            return (
              <div
                key={run.id}
                className="grid gap-2 py-4 text-sm sm:grid-cols-[1fr_auto]"
              >
                <div>
                  <span className="font-medium capitalize">{run.trigger}</span>
                  <span className="ml-3 text-[var(--color-text-tertiary)]">
                    {new Date(run.started_at).toLocaleString()}
                  </span>
                </div>
                <div>
                  {run.status} ·{' '}
                  {run.duration_sec === null
                    ? 'pending'
                    : `${run.duration_sec.toFixed(1)}s`}
                </div>
                {version ? (
                  <p className="font-mono text-xs text-[var(--color-text-tertiary)] sm:col-span-2">
                    {version.commit_sha.slice(0, 12)} · Graphify{' '}
                    {version.graphify_version} · {version.file_count} files ·{' '}
                    {version.node_count} nodes · {version.edge_count} edges
                  </p>
                ) : null}
                {run.error ? (
                  <p className="text-[var(--color-error)] sm:col-span-2">
                    {run.error}
                  </p>
                ) : null}
              </div>
            );
          })}
          {runs.length === 0 ? (
            <p className="py-6 text-sm text-[var(--color-text-tertiary)]">
              No sync attempts yet.
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
