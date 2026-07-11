'use client';

import { useEffect, useState, useCallback } from 'react';
import { Schedule, apiFetch } from '@/lib/api';

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const fetchSchedules = useCallback(() => {
    apiFetch<Schedule[]>('/api/schedules')
      .then(setSchedules)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchSchedules();
  }, [fetchSchedules]);

  const toggleSchedule = useCallback(async (schedule: Schedule) => {
    setTogglingId(schedule.id);
    try {
      const updated = await apiFetch<Schedule>(`/api/schedules/${schedule.id}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: !schedule.enabled }),
      });
      setSchedules((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch (error) {
      console.error(error);
    } finally {
      setTogglingId(null);
    }
  }, []);

  return (
    <div className="space-y-10">
      <section className="space-y-4 pt-4">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-3">
            <p className="text-[11px] uppercase tracking-[0.22em] text-[var(--color-text-tertiary)]">Schedules</p>
            <h1 className="font-display text-4xl font-normal leading-[1.15] tracking-tight text-[var(--color-text-primary)]">Cron-driven workflows, without the blind spots.</h1>
            <p className="max-w-2xl text-base leading-7 text-[var(--color-text-secondary)]">See which agents run on a cadence, what prompt they use, and when they are due next.</p>
          </div>
        </div>
      </section>

      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium text-[var(--color-text-primary)]">Active schedules</h2>
        <button
          onClick={fetchSchedules}
          className="rounded-btn border border-ops-border bg-ops-surface px-4 py-2 text-sm text-[var(--color-text-secondary)] transition-all hover:border-[var(--color-accent)]/30 hover:text-[var(--color-text-primary)]"
        >
          Refresh
        </button>
      </div>

      {loading ? (
        <p className="text-[var(--color-text-tertiary)]">Loading schedules...</p>
      ) : schedules.length === 0 ? (
        <div className="rounded-card border border-ops-border bg-ops-surface p-10 text-center">
          <p className="mb-2 text-[var(--color-text-secondary)]">No schedules configured</p>
          <p className="text-sm text-[var(--color-text-tertiary)]">
            Add a schedules section to your agent.yaml to define cron jobs.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {schedules.map((sched) => (
            <div
              key={sched.id}
              className="rounded-card border border-ops-border bg-ops-surface p-5"
            >
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div className="flex-1">
                  <div className="mb-3 flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={sched.enabled}
                      aria-label={`${sched.enabled ? 'Disable' : 'Enable'} ${sched.schedule_name}`}
                      disabled={togglingId === sched.id}
                      onClick={() => toggleSchedule(sched)}
                      className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors ${
                        sched.enabled ? 'bg-[var(--color-success)]' : 'bg-[var(--color-border)]'
                      } ${togglingId === sched.id ? 'opacity-60' : ''}`}
                    >
                      <span
                        className={`inline-block h-5 w-5 transform rounded-full bg-white transition-transform ${
                          sched.enabled ? 'translate-x-6' : 'translate-x-1'
                        }`}
                      />
                    </button>
                    <h3 className="text-base font-medium text-[var(--color-text-primary)]">{sched.schedule_name}</h3>
                    <span className="rounded-full border border-ops-border bg-ops-surface-raised px-2.5 py-0.5 text-[10px] text-[var(--color-text-secondary)]">{sched.agent_name}</span>
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-[10px] font-medium ${
                      sched.enabled
                        ? 'border border-[var(--color-success)]/20 bg-[var(--color-success-muted)] text-[var(--color-success)]'
                        : 'border border-ops-border bg-ops-surface-raised text-[var(--color-text-tertiary)]'
                    }`}
                  >
                    {sched.enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>
                <div className="flex flex-col gap-3 text-sm text-[var(--color-text-secondary)]">
                  <span className="w-fit rounded-btn border border-ops-border bg-ops-bg px-3 py-1.5 font-mono text-[var(--color-accent)]">
                    {sched.cron_expression}
                  </span>
                  <span className="leading-6 text-[var(--color-text-secondary)]">{sched.prompt}</span>
                </div>
                </div>
                <div className="grid gap-3 sm:grid-cols-2 xl:min-w-[280px] xl:grid-cols-1">
                  <ScheduleMeta label="Last run" value={sched.last_run_at ? new Date(sched.last_run_at).toLocaleString() : 'Never'} />
                  <ScheduleMeta label="Next run" value={sched.next_run_at ? new Date(sched.next_run_at).toLocaleString() : 'N/A'} />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ScheduleMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[10px] border border-ops-border-subtle bg-ops-bg px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-1 text-sm text-[var(--color-text-secondary)]">{value}</div>
    </div>
  );
}
