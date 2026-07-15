'use client';

import { usePathname } from 'next/navigation';
import { useState } from 'react';
import { useTheme } from '@/components/theme-provider';

const NAV_ITEMS = [
  { href: '/agents', label: 'Agents' },
  { href: '/tasks', label: 'Tasks' },
  { href: '/schedules', label: 'Schedules' },
  { href: '/mcp', label: 'MCP' },
  { href: '/connectors', label: 'Connectors' },
  { href: '/workflow-repo', label: 'Workflow Repo' },
  { href: '/approvals', label: 'Approvals' },
  { href: '/memory', label: 'Memory' },
  { href: '/analytics', label: 'Analytics' },
];

function isActive(pathname: string, href: string): boolean {
  if (href === '/') return pathname === '/';
  if (href === '/tasks')
    return pathname === '/tasks' || pathname.startsWith('/tasks/');
  return pathname === href || pathname.startsWith(`${href}/`);
}

function LogoIcon() {
  return (
    <svg
      width="36px"
      viewBox="0 0 40 36"
      role="presentation"
      xmlns="http://www.w3.org/2000/svg"
    >
      <line
        x1="20"
        y1="5"
        x2="35"
        y2="31"
        stroke="#C4622D"
        stroke-width="1.4"
        stroke-linecap="round"
      />
      <line
        x1="20"
        y1="5"
        x2="5"
        y2="31"
        stroke="#C4622D"
        stroke-width="1.4"
        stroke-linecap="round"
      />
      <line
        x1="5"
        y1="31"
        x2="35"
        y2="31"
        stroke="#C4622D"
        stroke-width="1.4"
        stroke-linecap="round"
      />
      <circle cx="20" cy="5" r="3.7" fill="#C4622D" />
      <circle cx="35" cy="31" r="3.7" fill="#C4622D" />
      <circle cx="5" cy="31" r="3.7" fill="#C4622D" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="5" />
      <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    >
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    >
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  );
}

export function TopNav() {
  const pathname = usePathname() || '/';
  const { theme, toggle } = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-40 border-b border-ops-border bg-[var(--nav-bg)] backdrop-blur-xl">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-4 py-3 sm:px-6 md:px-8 md:py-4">
        <a href="/" className="group flex items-center gap-2 no-underline">
          <LogoIcon />
          <div className="flex flex-col leading-none gap-[3px]">
            <span className="text-[9px] uppercase tracking-[0.18em] px-[3px] text-[var(--color-text-tertiary)] transition-colors group-hover:text-[var(--color-accent)]">
              Control Plane
            </span>
            <span className="font-sans text-[22px] leading-none">
              <span className="text-[#9B9184] font-light">Agentic </span>
              <span className="text-[var(--color-text-primary)] font-medium">
                Ops
              </span>
            </span>
          </div>
        </a>

        {/* Desktop nav */}
        <div className="hidden items-center gap-1 lg:flex">
          {NAV_ITEMS.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <a
                key={item.href}
                href={item.href}
                className={`rounded-[var(--radius-btn)] px-3.5 py-2 text-sm transition-all no-underline ${
                  active
                    ? 'bg-[var(--color-surface-raised)] text-[var(--color-text-primary)] font-medium'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface)] hover:text-[var(--color-text-primary)]'
                }`}
              >
                {item.label}
              </a>
            );
          })}
        </div>

        <div className="flex items-center gap-2">
          {/* Theme toggle */}
          <button
            onClick={toggle}
            aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
            className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-btn)] border border-ops-border text-[var(--color-text-secondary)] transition-all hover:bg-[var(--color-surface-raised)] hover:text-[var(--color-text-primary)]"
          >
            {theme === 'light' ? <MoonIcon /> : <SunIcon />}
          </button>

          {/* Mobile hamburger */}
          <button
            onClick={() => setMobileOpen(!mobileOpen)}
            className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-btn)] border border-ops-border text-[var(--color-text-secondary)] lg:hidden"
          >
            {mobileOpen ? <CloseIcon /> : <MenuIcon />}
          </button>
        </div>
      </div>

      {/* Mobile nav */}
      {mobileOpen && (
        <div className="border-t border-ops-border px-4 py-3 lg:hidden">
          <div className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <a
                  key={item.href}
                  href={item.href}
                  onClick={() => setMobileOpen(false)}
                  className={`rounded-[var(--radius-btn)] px-3.5 py-2.5 text-sm transition-all no-underline ${
                    active
                      ? 'bg-[var(--color-surface-raised)] text-[var(--color-text-primary)] font-medium'
                      : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface)] hover:text-[var(--color-text-primary)]'
                  }`}
                >
                  {item.label}
                </a>
              );
            })}
          </div>
        </div>
      )}
    </nav>
  );
}
