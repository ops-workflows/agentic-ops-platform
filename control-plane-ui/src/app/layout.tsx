import type { Metadata } from 'next';
import './globals.css';
import { TopNav } from '@/components/top-nav';
import { ThemeProvider } from '@/components/theme-provider';

export const metadata: Metadata = {
  title: 'Agentic Ops — Control Plane',
  description: 'Agent management, task queue, session replay, and analytics',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <div className="min-h-screen bg-ops-bg transition-colors">
            <TopNav />
            <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 md:px-8 md:py-10">{children}</main>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
