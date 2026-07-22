import {
  Brain,
  Cable,
  CalendarClock,
  ChartNoAxesCombined,
  ListTodo,
  PlugZap,
  ShieldCheck,
  Workflow,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

const DASHBOARD_AREAS: Array<{
  title: string;
  href: string;
  description: string;
  icon: LucideIcon;
}> = [
  {
    title: 'Workflows',
    href: '/workflows',
    description:
      'Manage workflow identities, runtime posture, and configuration shape.',
    icon: Workflow,
  },
  {
    title: 'Tasks',
    href: '/tasks',
    description:
      'Track queued, running, failed, and completed work with session replay.',
    icon: ListTodo,
  },
  {
    title: 'Schedules',
    href: '/schedules',
    description: 'Inspect cron-driven workflows and their execution cadence.',
    icon: CalendarClock,
  },
  {
    title: 'MCP',
    href: '/mcp',
    description:
      'Inspect MCP servers and drill into every published tool surface.',
    icon: PlugZap,
  },
  {
    title: 'Connectors',
    href: '/connectors',
    description:
      'Review ingestion connectors that feed workflows from external systems.',
    icon: Cable,
  },
  {
    title: 'Approvals',
    href: '/approvals',
    description:
      'Track pending and resolved operator approvals with clear audit fields.',
    icon: ShieldCheck,
  },
  {
    title: 'Memory',
    href: '/memory',
    description:
      'Explore Hindsight memory entries and readable agent memory archive files.',
    icon: Brain,
  },
  {
    title: 'Housekeeping',
    href: '/housekeeping',
    description:
      'Review background maintenance runs, retention warnings, and cleanup failures.',
    icon: Wrench,
  },
  {
    title: 'Analytics',
    href: '/analytics',
    description: 'Review throughput, duration, and token consumption trends.',
    icon: ChartNoAxesCombined,
  },
];

export default function Home() {
  return (
    <div className="space-y-10">
      <section className="space-y-4 pt-4">
        <h1 className="max-w-2xl font-display text-4xl font-normal leading-[1.15] tracking-tight text-[var(--color-text-primary)]">
          Operations overview
        </h1>
        <p className="max-w-xl text-base leading-7 text-[var(--color-text-secondary)]">
          Monitor live task execution, review traces and replay telemetry,
          manage schedules, and oversee approvals and governance.
        </p>
      </section>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {DASHBOARD_AREAS.map((area) => (
          <DashboardCard key={area.href} {...area} />
        ))}
      </div>
    </div>
  );
}

function DashboardCard({
  title,
  href,
  description,
  icon: Icon,
}: {
  title: string;
  href: string;
  description: string;
  icon: LucideIcon;
}) {
  return (
    <a
      href={href}
      className="group block rounded-card border border-ops-border bg-ops-surface p-6 transition-all duration-200 hover:border-[var(--color-accent)]/30 hover:shadow-card-hover"
    >
      <div className="mb-4 flex h-9 w-9 items-center justify-center rounded-[10px] border border-ops-border bg-ops-surface-raised text-[var(--color-accent)]">
        <Icon aria-hidden="true" size={19} strokeWidth={1.8} />
      </div>
      <h2 className="mb-2 text-lg font-medium text-[var(--color-text-primary)]">
        {title}
      </h2>
      <p className="text-sm leading-6 text-[var(--color-text-secondary)]">
        {description}
      </p>
      <div className="mt-5 text-xs font-medium text-[var(--color-accent)] transition-colors group-hover:text-[var(--color-accent-hover)]">
        Open →
      </div>
    </a>
  );
}
