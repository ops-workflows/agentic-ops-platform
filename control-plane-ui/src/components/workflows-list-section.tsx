import { Workflow } from 'lucide-react';
import type { Agent } from '@/lib/api';
import {
  getAgentModelBadgeClasses,
  getAgentModelInfo,
} from '@/lib/agent-model';
import { ExpandableMeta } from '@/components/expandable-meta';
import { formatWorkflowDate } from '@/lib/workflow-format';

export function WorkflowsListSection({
  agents,
  loading,
  togglingAgent,
  onToggleAgentPaused,
}: {
  agents: Agent[];
  loading: boolean;
  togglingAgent: string | null;
  onToggleAgentPaused: (agent: Agent) => void;
}) {
  return (
    <section className="border-t border-ops-border pt-8">
      <h2 className="text-lg font-medium text-[var(--color-text-primary)]">
        Workflows
      </h2>

      {loading ? (
        <p className="mt-4 text-[var(--color-text-tertiary)]">
          Loading workflows...
        </p>
      ) : agents.length === 0 ? (
        <p className="mt-4 text-[var(--color-text-tertiary)]">
          No workflows found.
        </p>
      ) : (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          {agents.map((agent) => {
            const modelInfo = getAgentModelInfo(agent.config);

            return (
              <div
                key={agent.id}
                className="group rounded-card border border-ops-border bg-ops-surface p-6 no-underline transition-all duration-200 hover:border-[var(--color-accent)]/30 hover:shadow-card-hover"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1 space-y-3">
                    <div className="flex items-center gap-3">
                      <WorkflowIcon />
                      <div className="min-w-0">
                        <div className="text-base font-medium text-[var(--color-text-primary)]">
                          {agent.name}
                        </div>
                      </div>
                    </div>
                    <p className="min-h-[72px] text-sm leading-6 text-[var(--color-text-secondary)]">
                      {agent.description || 'No description provided.'}
                    </p>
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-2">
                    {modelInfo && (
                      <span
                        className={`border px-2 py-1 text-[10px] font-bold uppercase tracking-[0.12em] ${getAgentModelBadgeClasses(modelInfo.tone)}`}
                      >
                        {modelInfo.label}
                      </span>
                    )}
                    <span
                      className={`border border-current/40 px-2 py-1 text-[10px] font-bold uppercase tracking-[0.12em] ${agent.paused ? 'text-[var(--color-warning)]' : 'text-[var(--color-success)]'}`}
                    >
                      {agent.paused ? 'Paused' : 'Active'}
                    </span>
                  </div>
                </div>

                <div className="mt-5 grid gap-2 sm:grid-cols-3">
                  <ExpandableMeta
                    label="Path"
                    value={agent.repo_path || 'n/a'}
                  />
                  <ExpandableMeta
                    label="Updated"
                    value={formatWorkflowDate(agent.updated)}
                  />
                  <ExpandableMeta
                    label="Schedules"
                    value={String(
                      (
                        ((agent.config as Record<string, unknown>).schedules as
                          | Array<unknown>
                          | undefined) || []
                      ).length,
                    )}
                  />
                </div>

                <div className="mt-5 flex items-center justify-between">
                  <button
                    type="button"
                    onClick={() => onToggleAgentPaused(agent)}
                    disabled={togglingAgent === agent.id}
                    className={`min-w-[112px] rounded-btn border px-4 py-2 text-sm transition-all ${agent.paused ? 'border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)] hover:bg-[var(--color-success)]/20' : 'border-[var(--color-warning)]/20 bg-[var(--color-warning-muted)] text-[var(--color-warning)] hover:bg-[var(--color-warning)]/20'} disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    {togglingAgent === agent.id
                      ? agent.paused
                        ? 'Resuming…'
                        : 'Pausing…'
                      : agent.paused
                        ? 'Resume'
                        : 'Pause'}
                  </button>
                  <a
                    href={`/workflows/${agent.name}`}
                    className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
                  >
                    Open workflow
                  </a>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function WorkflowIcon() {
  return (
    <div className="flex h-9 w-9 flex-none items-center justify-center rounded-[10px] border border-ops-border bg-ops-surface-raised text-[var(--color-accent)]">
      <Workflow aria-hidden="true" size={19} strokeWidth={1.8} />
    </div>
  );
}
