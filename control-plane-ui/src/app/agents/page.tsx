'use client';

import { useEffect, useState } from 'react';
import { Agent, apiFetch } from '@/lib/api';
import { getAgentModelBadgeClasses, getAgentModelInfo } from '@/lib/agent-model';

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [togglingAgent, setTogglingAgent] = useState<string | null>(null);

  useEffect(() => {
    loadAgents();
  }, []);

  async function loadAgents() {
    setLoading(true);
    try {
      const data = await apiFetch<Agent[]>('/api/agents');
      setAgents(data);
    } catch (e) {
      console.error('Failed to load agents:', e);
    }
    setLoading(false);
  }

  async function toggleAgentPaused(agent: Agent) {
    setTogglingAgent(agent.id);
    try {
      const updated = await apiFetch<Agent>(`/api/agents/${agent.name}/${agent.paused ? 'resume' : 'pause'}`, {
        method: 'POST',
      });
      setAgents((current) => current.map((entry) => (entry.id === updated.id ? updated : entry)));
    } catch (e) {
      console.error(`Failed to ${agent.paused ? 'resume' : 'pause'} agent:`, e);
    } finally {
      setTogglingAgent(null);
    }
  }

  const totalSchedules = agents.reduce((sum, agent) => {
    const config = agent.config as Record<string, unknown>;
    return sum + (((config.schedules as Array<unknown> | undefined) || []).length);
  }, 0);

  const activeCount = agents.filter((a) => !a.paused).length;

  return (
    <div className="space-y-10">
      <section className="space-y-4 pt-4">
        <div className="flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="max-w-2xl space-y-4">
            <p className="text-[11px] uppercase tracking-[0.22em] text-[var(--color-text-tertiary)]">Agents</p>
            <h1 className="font-display text-4xl font-normal leading-[1.15] tracking-tight text-[var(--color-text-primary)]">A live catalog of the workflows shaping the platform.</h1>
            <p className="text-base leading-7 text-[var(--color-text-secondary)]">
              Browse provisioned plugins, inspect their runtime footprint, and review the deployed workflow surface.
            </p>
          </div>
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <StatCard label="Provisioned" value={agents.length} />
          <StatCard label="Active" value={activeCount} />
          <StatCard label="Schedules" value={totalSchedules} />
        </div>
      </section>

      <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Registry</h2>

      {loading ? (
        <p className="text-[var(--color-text-tertiary)]">Loading agents...</p>
      ) : agents.length === 0 ? (
        <p className="text-[var(--color-text-tertiary)]">No agents found. Deploy plugins to the plugins directory.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {agents.map((agent) => {
            const modelInfo = getAgentModelInfo(agent.config);

            return (
            <div
              key={agent.id}
              className="group rounded-card border border-ops-border bg-ops-surface p-6 no-underline transition-all duration-200 hover:border-[var(--color-accent)]/30 hover:shadow-card-hover"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-3 min-w-0 flex-1">
                  <div className="flex items-center gap-3">
                    <AgentIcon name={agent.name} />
                    <div className="min-w-0">
                      <div className="text-base font-medium text-[var(--color-text-primary)]">{agent.name}</div>
                      <div className="text-[10px] text-[var(--color-text-tertiary)]">v{agent.version || '0.0'}</div>
                    </div>
                  </div>
                  <p className="text-sm leading-6 text-[var(--color-text-secondary)]">{agent.description || 'No description provided.'}</p>
                </div>
                <div className="flex flex-shrink-0 items-center gap-2">
                  {modelInfo && (
                    <span className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${getAgentModelBadgeClasses(modelInfo.tone)}`}>
                      {modelInfo.label}
                    </span>
                  )}
                  <span className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${agent.paused ? 'border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)]' : 'border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)]'}`}>
                    {agent.paused ? 'Paused' : 'Active'}
                  </span>
                </div>
              </div>

              <div className="mt-5 grid gap-2 sm:grid-cols-3">
                <MetaPill label="Path" value={agent.repo_path || 'n/a'} />
                <MetaPill label="Updated" value={new Date(agent.updated).toLocaleDateString()} />
                <MetaPill label="Schedules" value={String((((agent.config as Record<string, unknown>).schedules as Array<unknown> | undefined) || []).length)} />
              </div>

              <div className="mt-5 flex items-center justify-between">
                <button
                  type="button"
                  onClick={() => toggleAgentPaused(agent)}
                  disabled={togglingAgent === agent.id}
                  className={`rounded-btn px-3 py-1.5 text-xs font-medium transition-all ${agent.paused ? 'border border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)] hover:bg-[var(--color-success)]/20' : 'border border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)] hover:bg-[var(--color-warning)]/20'} disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  {togglingAgent === agent.id ? (agent.paused ? 'Resuming…' : 'Pausing…') : agent.paused ? 'Resume' : 'Pause'}
                </button>
                <a
                  href={`/agents/${agent.name}`}
                  className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
                >
                  Open details
                </a>
              </div>
            </div>
          );})}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-card border border-ops-border bg-ops-surface px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-2 text-2xl font-medium text-[var(--color-text-primary)]">{value}</div>
    </div>
  );
}

function MetaPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[10px] border border-ops-border-subtle bg-ops-bg px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-1 truncate text-sm text-[var(--color-text-secondary)]" title={value}>{value}</div>
    </div>
  );
}

function AgentIcon({ name }: { name: string }) {
  /* Generate a deterministic hue from the agent name */
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  const hue = Math.abs(hash) % 360;
  const color = `hsl(${hue}, 45%, 55%)`;

  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg" className="flex-none">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <circle cx="18" cy="14" r="4" stroke={color} strokeWidth="1.8" fill="none"/>
      <path d="M11 26c0-3.87 3.13-7 7-7s7 3.13 7 7" stroke={color} strokeWidth="1.8" strokeLinecap="round" fill="none"/>
      <circle cx="24" cy="12" r="2" fill={color} opacity="0.4"/>
    </svg>
  );
}
