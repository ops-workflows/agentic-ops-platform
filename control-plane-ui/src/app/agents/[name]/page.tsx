'use client';

import { useEffect, useState } from 'react';
import { type Agent, type AgentFilesResult, apiFetch } from '@/lib/api';
import {
  getAgentModelBadgeClasses,
  getAgentModelInfo,
} from '@/lib/agent-model';
import { PluginPreview } from '@/components/plugin-preview';
import { useParams } from 'next/navigation';

type Tab = 'preview' | 'config' | 'secrets' | 'schedules';

interface SecretItem {
  name: string;
  description: string | null;
  has_value: boolean;
}

export default function AgentDetailPage() {
  const params = useParams();
  const nameParam = params?.name;
  const name = Array.isArray(nameParam) ? nameParam[0] : (nameParam ?? '');
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tab, setTab] = useState<Tab>('preview');
  const [loading, setLoading] = useState(true);
  const [secrets, setSecrets] = useState<SecretItem[]>([]);
  const [pluginFiles, setPluginFiles] = useState<AgentFilesResult | null>(null);
  const [pluginFilesLoading, setPluginFilesLoading] = useState(false);

  useEffect(() => {
    setAgent(null);
    setTab('preview');
    setSecrets([]);
    setPluginFiles(null);
    setPluginFilesLoading(false);
    setLoading(true);
  }, [name]);

  useEffect(() => {
    if (!name) {
      setLoading(false);
      return;
    }
    apiFetch<Agent>(`/api/agents/${name}`)
      .then(setAgent)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [name]);

  const loadSecrets = () => {
    if (!name) return;
    apiFetch<SecretItem[]>(`/api/agents/${name}/secrets`)
      .then(setSecrets)
      .catch(console.error);
  };

  const loadPluginFiles = () => {
    if (!name || pluginFiles || pluginFilesLoading) return;
    setPluginFilesLoading(true);
    apiFetch<AgentFilesResult>(`/api/agents/${name}/files`)
      .then(setPluginFiles)
      .catch(console.error)
      .finally(() => setPluginFilesLoading(false));
  };

  useEffect(() => {
    if (tab === 'secrets') loadSecrets();
  }, [tab, name]);

  useEffect(() => {
    if (tab === 'preview') loadPluginFiles();
  }, [tab, name, pluginFiles, pluginFilesLoading]);

  if (loading)
    return (
      <p className="text-[var(--color-text-tertiary)]">Loading agent...</p>
    );
  if (!name)
    return (
      <p className="text-[var(--color-text-tertiary)]">Invalid agent route</p>
    );
  if (!agent)
    return <p className="text-[var(--color-text-tertiary)]">Agent not found</p>;

  const config = agent.config as Record<string, unknown>;
  const modelInfo = getAgentModelInfo(config);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="font-display text-3xl font-normal tracking-tight text-[var(--color-text-primary)]">
              {agent.name}
            </h1>
            <div className="flex items-center gap-2">
              {modelInfo && (
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${getAgentModelBadgeClasses(modelInfo.tone)}`}
                >
                  {modelInfo.label}
                </span>
              )}
              <span
                className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${agent.paused ? 'border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)]' : 'border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)]'}`}
              >
                {agent.paused ? 'Paused' : 'Active'}
              </span>
            </div>
          </div>
          <p className="text-[var(--color-text-secondary)] mt-1">
            {agent.description}
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-8 border-b border-ops-border">
        {(['preview', 'config', 'secrets', 'schedules'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === t
                ? 'border-[var(--color-accent)] text-[var(--color-text-primary)]'
                : 'border-transparent text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)]'
            }`}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {tab === 'preview' &&
        (pluginFilesLoading && !pluginFiles ? (
          <div className="bg-ops-surface border border-ops-border rounded-card p-5 text-sm text-[var(--color-text-tertiary)]">
            Loading plugin files...
          </div>
        ) : (
          <PluginPreview
            agentYaml={pluginFiles?.agent_yaml || ''}
            files={pluginFiles?.files || {}}
            emptyMessage="This agent has no plugin directory files available yet."
          />
        ))}

      {tab === 'config' && (
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <pre className="text-sm text-[var(--color-text-secondary)] overflow-auto max-h-[600px] font-mono">
            {JSON.stringify(config, null, 2)}
          </pre>
        </div>
      )}

      {tab === 'secrets' && (
        <SecretsPanel
          agentName={name}
          secrets={secrets}
          onRefresh={loadSecrets}
        />
      )}

      {tab === 'schedules' && (
        <div className="space-y-3">
          {((config.schedules as Array<Record<string, unknown>>) || []).map(
            (sched, i) => (
              <div
                key={i}
                className="bg-ops-surface border border-ops-border rounded-card p-5"
              >
                <h3 className="font-medium text-[var(--color-text-primary)]">
                  {sched.name as string}
                </h3>
                <p className="text-sm text-[var(--color-text-secondary)] mt-1">
                  Cron:{' '}
                  <span className="font-mono text-[var(--color-accent)]">
                    {sched.cron as string}
                  </span>
                </p>
                <p className="text-sm text-[var(--color-text-secondary)]">
                  {sched.prompt as string}
                </p>
              </div>
            ),
          )}
          {!((config.schedules as unknown[]) || []).length && (
            <p className="text-[var(--color-text-tertiary)]">
              No schedules configured
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SecretsPanel({
  agentName,
  secrets,
  onRefresh,
}: {
  agentName: string;
  secrets: SecretItem[];
  onRefresh: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [newName, setNewName] = useState('');
  const [newValue, setNewValue] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [busy, setBusy] = useState(false);

  const handleSave = async (
    key: string,
    value: string,
    description?: string,
  ) => {
    setBusy(true);
    try {
      await apiFetch(`/api/agents/${agentName}/secrets/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value, description: description || undefined }),
      });
      onRefresh();
      setAdding(false);
      setEditingKey(null);
      setNewName('');
      setNewValue('');
      setNewDesc('');
    } catch (e) {
      console.error('Failed to save secret', e);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (key: string) => {
    if (!confirm(`Delete secret "${key}"?`)) return;
    setBusy(true);
    try {
      await apiFetch(`/api/agents/${agentName}/secrets/${key}`, {
        method: 'DELETE',
      });
      onRefresh();
    } catch (e) {
      console.error('Failed to delete secret', e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-[var(--color-text-tertiary)]">
          Encrypted secrets stored in agent.yaml. Values are never displayed.
        </p>
        <button
          onClick={() => setAdding(true)}
          className="px-3 py-1.5 bg-[var(--color-accent)] text-white text-sm rounded-btn hover:bg-[var(--color-accent-hover)] transition-colors"
          disabled={adding}
        >
          + Add Secret
        </button>
      </div>

      {/* Secret list */}
      <div className="space-y-2">
        {secrets.map((s) => (
          <div
            key={s.name}
            className="bg-ops-surface border border-ops-border rounded-card p-4 flex items-center justify-between"
          >
            <div>
              <h3 className="font-mono text-sm font-medium text-[var(--color-text-primary)]">
                {s.name}
              </h3>
              {s.description && (
                <p className="text-xs text-[var(--color-text-tertiary)] mt-1">
                  {s.description}
                </p>
              )}
            </div>
            <div className="flex items-center gap-3">
              <span className="text-sm text-[var(--color-text-tertiary)] font-mono">
                {s.has_value ? '●●●●●●●●' : '(empty)'}
              </span>
              {editingKey === s.name ? (
                <div className="flex gap-2">
                  <input
                    type="password"
                    placeholder="New value"
                    className="px-2 py-1 bg-ops-bg border border-ops-border rounded-input text-sm text-[var(--color-text-primary)] focus:outline-none focus:border-[var(--color-accent)]"
                    value={newValue}
                    onChange={(e) => setNewValue(e.target.value)}
                  />
                  <button
                    onClick={() => handleSave(s.name, newValue)}
                    disabled={busy || !newValue}
                    className="px-2 py-1 bg-[var(--color-success)] text-white text-xs rounded-btn"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => {
                      setEditingKey(null);
                      setNewValue('');
                    }}
                    className="px-2 py-1 bg-ops-surface-raised text-[var(--color-text-secondary)] text-xs rounded-btn"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      setEditingKey(s.name);
                      setNewValue('');
                    }}
                    className="px-2 py-1 bg-ops-surface-raised text-[var(--color-text-secondary)] text-xs rounded-btn hover:text-[var(--color-text-primary)] transition-colors"
                  >
                    Update
                  </button>
                  <button
                    onClick={() => handleDelete(s.name)}
                    disabled={busy}
                    className="px-2 py-1 bg-[var(--color-error)]/10 text-[var(--color-error)] text-xs rounded-btn hover:bg-[var(--color-error)]/20 transition-colors"
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}

        {secrets.length === 0 && !adding && (
          <p className="text-[var(--color-text-tertiary)] text-sm">
            No secrets configured for this agent.
          </p>
        )}
      </div>

      {/* Add new secret form */}
      {adding && (
        <div className="bg-ops-surface border border-ops-border rounded-card p-5 space-y-3">
          <h3 className="text-sm font-medium text-[var(--color-text-primary)]">
            New Secret
          </h3>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-[var(--color-text-tertiary)]">
                Name (env var)
              </label>
              <input
                type="text"
                placeholder="API_TOKEN"
                className="w-full px-2 py-1.5 bg-ops-bg border border-ops-border rounded-input text-sm font-mono text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:border-[var(--color-accent)]"
                value={newName}
                onChange={(e) =>
                  setNewName(
                    e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ''),
                  )
                }
              />
            </div>
            <div>
              <label className="text-xs text-[var(--color-text-tertiary)]">
                Value
              </label>
              <input
                type="password"
                placeholder="Secret value"
                className="w-full px-2 py-1.5 bg-ops-bg border border-ops-border rounded-input text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:border-[var(--color-accent)]"
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="text-xs text-[var(--color-text-tertiary)]">
              Description (optional)
            </label>
            <input
              type="text"
              placeholder="What this secret is for"
              className="w-full px-2 py-1.5 bg-ops-bg border border-ops-border rounded-input text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:border-[var(--color-accent)]"
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => handleSave(newName, newValue, newDesc)}
              disabled={busy || !newName || !newValue}
              className="px-3 py-1.5 bg-[var(--color-accent)] text-white text-sm rounded-btn hover:bg-[var(--color-accent-hover)] disabled:opacity-50 transition-colors"
            >
              {busy ? 'Encrypting...' : 'Encrypt & Save'}
            </button>
            <button
              onClick={() => {
                setAdding(false);
                setNewName('');
                setNewValue('');
                setNewDesc('');
              }}
              className="px-3 py-1.5 bg-ops-surface-raised text-[var(--color-text-secondary)] text-sm rounded-btn"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
