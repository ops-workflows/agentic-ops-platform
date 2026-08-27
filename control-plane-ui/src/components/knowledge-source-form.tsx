'use client';

import type { GitHubConnection } from '@/lib/api';

export const EMPTY_KNOWLEDGE_SOURCE_FORM = {
  repository: '',
  default_ref: 'main',
  include_paths: '',
  exclude_paths: '',
  credential_ref: '',
  schedule: 'daily',
  enabled: true,
};

export type KnowledgeSourceFormValue = typeof EMPTY_KNOWLEDGE_SOURCE_FORM;

export function sourcePayload(form: KnowledgeSourceFormValue) {
  const intervalSec = { hourly: 3600, daily: 86400, weekly: 604800 }[
    form.schedule
  ];
  return {
    repository: form.repository,
    default_ref: form.default_ref,
    include_paths: splitLines(form.include_paths),
    exclude_paths: splitLines(form.exclude_paths),
    credential_ref: form.credential_ref,
    sync_policy: intervalSec ? { interval_sec: intervalSec } : {},
    enabled: form.enabled,
  };
}

export function KnowledgeSourceForm({
  form,
  setForm,
  onSubmit,
  submitLabel,
  connections,
}: {
  form: KnowledgeSourceFormValue;
  setForm: React.Dispatch<React.SetStateAction<KnowledgeSourceFormValue>>;
  onSubmit: (event: React.FormEvent) => void;
  submitLabel: string;
  connections: GitHubConnection[];
}) {
  const field = (name: 'repository' | 'default_ref') => ({
    value: form[name],
    onChange: (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm((current) => ({ ...current, [name]: event.target.value })),
  });
  const multilineField = (name: 'include_paths' | 'exclude_paths') => ({
    value: form[name],
    onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) =>
      setForm((current) => ({ ...current, [name]: event.target.value })),
  });
  const selectField = (name: 'credential_ref' | 'schedule') => ({
    value: form[name],
    onChange: (event: React.ChangeEvent<HTMLSelectElement>) =>
      setForm((current) => ({ ...current, [name]: event.target.value })),
  });
  return (
    <form
      onSubmit={onSubmit}
      className="grid gap-5 rounded-card border border-ops-border bg-ops-surface p-5 shadow-card sm:p-6 md:grid-cols-2"
    >
      <Input
        label="Repository (organization/repository)"
        required
        {...field('repository')}
      />
      <Select
        label="GitHub connection"
        required
        {...selectField('credential_ref')}
      >
        <option value="">Select connection</option>
        {connections.map((connection) => (
          <option key={connection.name} value={connection.name}>
            {connection.name} ({connection.web_base_url})
          </option>
        ))}
      </Select>
      <Input label="Default ref" required {...field('default_ref')} />
      <Select label="Sync schedule" {...selectField('schedule')}>
        <option value="">Manual only</option>
        <option value="hourly">Hourly</option>
        <option value="daily">Daily</option>
        <option value="weekly">Weekly</option>
      </Select>
      <Textarea
        label="Include globs, one per line"
        {...multilineField('include_paths')}
      />
      <Textarea
        label="Exclude globs, one per line"
        {...multilineField('exclude_paths')}
      />
      <div className="flex items-end md:col-span-2">
        <button
          type="submit"
          className="rounded-btn bg-[var(--color-accent)] px-4 py-2.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-[var(--color-accent-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)] focus:ring-offset-2 focus:ring-offset-ops-surface"
        >
          {submitLabel}
        </button>
      </div>
    </form>
  );
}

function Select({
  label,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement> & { label: string }) {
  return (
    <label className="space-y-1.5 text-xs font-medium text-[var(--color-text-secondary)]">
      <span>{label}</span>
      <select
        {...props}
        className="w-full appearance-none rounded-btn border border-ops-border bg-ops-bg px-3 py-2.5 text-sm text-[var(--color-text-primary)] shadow-sm outline-none transition-colors hover:border-[var(--color-text-tertiary)] focus:border-[var(--color-accent)] focus:ring-2 focus:ring-[var(--color-accent)]/20"
      >
        {children}
      </select>
    </label>
  );
}

function Textarea({
  label,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { label: string }) {
  return (
    <label className="space-y-1.5 text-xs font-medium text-[var(--color-text-secondary)]">
      <span>{label}</span>
      <textarea
        {...props}
        rows={3}
        className="w-full resize-y rounded-btn border border-ops-border bg-ops-bg px-3 py-2.5 text-sm text-[var(--color-text-primary)] shadow-sm outline-none transition-colors placeholder:text-[var(--color-text-tertiary)] hover:border-[var(--color-text-tertiary)] focus:border-[var(--color-accent)] focus:ring-2 focus:ring-[var(--color-accent)]/20"
      />
    </label>
  );
}

function Input({
  label,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  return (
    <label className="space-y-1.5 text-xs font-medium text-[var(--color-text-secondary)]">
      <span>{label}</span>
      <input
        {...props}
        className="w-full rounded-btn border border-ops-border bg-ops-bg px-3 py-2.5 text-sm text-[var(--color-text-primary)] shadow-sm outline-none transition-colors placeholder:text-[var(--color-text-tertiary)] hover:border-[var(--color-text-tertiary)] focus:border-[var(--color-accent)] focus:ring-2 focus:ring-[var(--color-accent)]/20"
      />
    </label>
  );
}

function splitLines(value: string) {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function repositoryName(repository: string) {
  return (
    repository
      .replace(/\.git$/, '')
      .split('/')
      .pop() || repository
  );
}
