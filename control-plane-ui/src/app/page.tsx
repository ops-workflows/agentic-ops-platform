export default function Home() {
  return (
    <div className="space-y-10">
      <section className="space-y-4 pt-4">
        <p className="text-[11px] uppercase tracking-[0.22em] text-[var(--color-text-tertiary)]">Operational Command</p>
        <h1 className="max-w-2xl font-display text-4xl font-normal leading-[1.15] tracking-tight text-[var(--color-text-primary)]">Run agents, inspect traces, and keep workflow automations honest.</h1>
        <p className="max-w-xl text-base leading-7 text-[var(--color-text-secondary)]">
          The control plane is where live task execution, replay telemetry, schedules, and governance surfaces meet.
        </p>
      </section>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <DashboardCard title="Agents" href="/agents" description="Manage plugin identities, runtime posture, and configuration shape." icon={<AgentsIcon />} />
        <DashboardCard title="Tasks" href="/tasks" description="Track queued, running, failed, and completed work with session replay." icon={<TasksIcon />} />
        <DashboardCard title="Schedules" href="/schedules" description="Inspect cron-driven workflows and their execution cadence." icon={<SchedulesIcon />} />
        <DashboardCard title="MCP" href="/mcp" description="Inspect MCP servers and drill into every published tool surface." icon={<PlatformIcon />} />
        <DashboardCard title="Connectors" href="/connectors" description="Review ingestion connectors that feed workflows from external systems." icon={<ConnectorsIcon />} />
        <DashboardCard title="Approvals" href="/approvals" description="Track pending and resolved operator approvals with clear audit fields." icon={<ApprovalsIcon />} />
        <DashboardCard title="Memory" href="/memory" description="Explore Hindsight memory entries and readable agent memory archive files." icon={<MemoryIcon />} />
        <DashboardCard title="Housekeeping" href="/housekeeping" description="Review background maintenance runs, retention warnings, and cleanup failures." icon={<HousekeepingIcon />} />
        <DashboardCard title="Analytics" href="/analytics" description="Review throughput, duration, and token consumption trends." icon={<AnalyticsIcon />} />
      </div>
    </div>
  );
}

function DashboardCard({
  title,
  href,
  description,
  icon,
}: {
  title: string;
  href: string;
  description: string;
  icon: React.ReactNode;
}) {
  return (
    <a
      href={href}
      className="group block rounded-card border border-ops-border bg-ops-surface p-6 transition-all duration-200 hover:border-[var(--color-accent)]/30 hover:shadow-card-hover"
    >
      <div className="mb-4">{icon}</div>
      <h2 className="mb-2 text-lg font-medium text-[var(--color-text-primary)]">{title}</h2>
      <p className="text-sm leading-6 text-[var(--color-text-secondary)]">{description}</p>
      <div className="mt-5 text-xs font-medium text-[var(--color-accent)] transition-colors group-hover:text-[var(--color-accent-hover)]">Open →</div>
    </a>
  );
}

/* ── Dashboard Card SVG Icons ── */

function AgentsIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <circle cx="18" cy="14" r="4" stroke="var(--color-accent)" strokeWidth="1.8" fill="none"/>
      <path d="M11 26c0-3.87 3.13-7 7-7s7 3.13 7 7" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round" fill="none"/>
      <circle cx="25" cy="12" r="2" fill="var(--color-accent)" opacity="0.4"/>
    </svg>
  );
}

function TasksIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <rect x="11" y="10" width="14" height="16" rx="2" stroke="var(--color-accent)" strokeWidth="1.8" fill="none"/>
      <path d="M15 15h6M15 19h6M15 23h4" stroke="var(--color-accent)" strokeWidth="1.5" strokeLinecap="round" opacity="0.7"/>
    </svg>
  );
}

function SchedulesIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <circle cx="18" cy="18" r="7" stroke="var(--color-accent)" strokeWidth="1.8" fill="none"/>
      <path d="M18 14v4l3 2" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="18" cy="18" r="1" fill="var(--color-accent)"/>
    </svg>
  );
}

function AnalyticsIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <path d="M12 24V18M16 24V14M20 24V16M24 24V12" stroke="var(--color-accent)" strokeWidth="2" strokeLinecap="round"/>
    </svg>
  );
}

function ConnectorsIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <path d="M11 14.5h14l-7 5-7-5Z" stroke="var(--color-accent)" strokeWidth="1.7" strokeLinejoin="round" fill="none"/>
      <path d="M11 14.5V24h14v-9.5" stroke="var(--color-accent)" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" opacity="0.78"/>
      <path d="M18 8v6" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round"/>
      <path d="M15.5 11.5 18 14l2.5-2.5" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="26" cy="11" r="2" fill="var(--color-accent)" opacity="0.35"/>
    </svg>
  );
}

function ApprovalsIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <circle cx="18" cy="18" r="8" stroke="var(--color-accent)" strokeWidth="1.8" fill="none"/>
      <path d="M14.2 18.3l2.5 2.7 5.4-6" stroke="var(--color-accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="24.8" cy="12" r="1.8" fill="var(--color-accent)" opacity="0.35"/>
    </svg>
  );
}

function MemoryIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <path d="M18 10c-4.3 0-7.4 3-7.4 6.8 0 2.2 1.1 4.1 2.8 5.4v2.1c0 .9.7 1.7 1.7 1.7h5.8c.9 0 1.7-.8 1.7-1.7v-2.1c1.7-1.3 2.8-3.2 2.8-5.4 0-3.8-3.1-6.8-7.4-6.8Z" stroke="var(--color-accent)" strokeWidth="1.7" fill="none"/>
      <path d="M15.6 16.5c.7-1.4 2-2.2 3.8-2.2 1.2 0 2.2.4 3 .9" stroke="var(--color-accent)" strokeWidth="1.5" strokeLinecap="round" opacity="0.75"/>
      <path d="M15.4 28h5.2" stroke="var(--color-accent)" strokeWidth="1.7" strokeLinecap="round"/>
      <path d="M17 22.2v-2.4m2 2.4v-3.8" stroke="var(--color-accent)" strokeWidth="1.6" strokeLinecap="round" opacity="0.8"/>
    </svg>
  );
}

function PlatformIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <circle cx="11.5" cy="11.5" r="2.25" fill="var(--color-accent)"/>
      <circle cx="24.5" cy="11.5" r="2.25" fill="var(--color-accent)" opacity="0.75"/>
      <circle cx="18" cy="24.5" r="2.25" fill="var(--color-accent)" opacity="0.55"/>
      <path d="M13.2 12.6h9.6M12.9 13.2l4 8.1M23.1 13.2l-4 8.1" stroke="var(--color-accent)" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function HousekeepingIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="36" height="36" rx="10" fill="var(--color-surface-raised)" stroke="var(--color-border)" strokeWidth="1"/>
      <path d="M13 24.5 20.8 13" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round"/>
      <path d="M20.3 13h6.2" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round"/>
      <path d="M11 25h12.5" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinecap="round" opacity="0.75"/>
      <path d="M10.8 20.5h7.4" stroke="var(--color-accent)" strokeWidth="1.5" strokeLinecap="round" opacity="0.55"/>
      <circle cx="25.5" cy="13" r="2" fill="var(--color-accent)" opacity="0.35"/>
    </svg>
  );
}
