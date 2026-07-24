export function ExpandableMeta({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div
      className="group/meta relative min-h-[62px] rounded-btn border border-ops-border-subtle bg-ops-bg px-3 py-2.5 outline-none focus:ring-1 focus:ring-[var(--color-info)]"
      tabIndex={0}
    >
      <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className="mt-1 truncate text-sm text-[var(--color-text-secondary)] group-hover/meta:hidden group-focus/meta:hidden">
        {value}
      </div>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-x-px -top-px z-10 hidden min-h-[calc(100%+2px)] break-words rounded-btn border border-ops-border-subtle bg-ops-bg px-3 py-2.5 text-sm text-[var(--color-text-secondary)] shadow-card group-hover/meta:block group-focus/meta:block"
      >
        <div className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
          {label}
        </div>
        <div className="mt-1">{value}</div>
      </div>
    </div>
  );
}
