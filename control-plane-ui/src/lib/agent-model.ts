type AgentModelTone = 'neutral' | 'sky';

export interface AgentModelInfo {
  label: string;
  tone: AgentModelTone;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function formatModelLabel(model: string): string {
  if (!model) return model;
  return model.charAt(0).toUpperCase() + model.slice(1);
}

export function getAgentModelInfo(config: Record<string, unknown>): AgentModelInfo | null {
  const session = isRecord(config.session) ? config.session : null;
  const rawModel = typeof session?.model === 'string' ? session.model.trim().toLowerCase() : '';

  if (!rawModel) {
    return null;
  }

  if (rawModel === 'gemini') {
    return { label: 'Gemini', tone: 'sky' };
  }

  if (rawModel === 'local') {
    return { label: 'Local', tone: 'neutral' };
  }

  return {
    label: formatModelLabel(rawModel),
    tone: 'neutral',
  };
}

export function getAgentModelBadgeClasses(tone: AgentModelTone): string {
  if (tone === 'sky') {
    return 'border-[var(--color-info)]/20 bg-[var(--color-info-muted)] text-[var(--color-info)]';
  }

  return 'border-ops-border bg-ops-surface-raised text-[var(--color-text-secondary)]';
}