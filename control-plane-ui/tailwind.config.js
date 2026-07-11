/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      colors: {
        /* ── Warm editorial dark palette ── */
        ops: {
          bg:             'var(--color-bg)',
          surface:        'var(--color-surface)',
          'surface-raised': 'var(--color-surface-raised)',
          border:         'var(--color-border)',
          'border-subtle': 'var(--color-border-subtle)',
          primary:        'var(--color-accent)',
          secondary:      'var(--color-accent-secondary)',
          success:        'var(--color-success)',
          warning:        'var(--color-warning)',
          danger:         'var(--color-error)',
        },
      },
      fontFamily: {
        display: ['Georgia', '"Times New Roman"', 'serif'],
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"SF Mono"', '"Fira Code"', 'monospace'],
      },
      borderRadius: {
        'card': '16px',
        'btn': '10px',
        'input': '10px',
      },
      boxShadow: {
        'card': 'var(--shadow-card)',
        'card-hover': 'var(--shadow-card-hover)',
        'subtle': '0 1px 2px rgba(0,0,0,0.05)',
      },
    },
  },
  plugins: [],
};
