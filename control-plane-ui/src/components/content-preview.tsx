'use client';

import { Highlight, themes } from 'prism-react-renderer';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTheme } from '@/components/theme-provider';

export type CodeLanguage = 'json' | 'yaml' | 'text';

export function SyntaxCode({
  code,
  language,
  className,
}: {
  code: string;
  language: CodeLanguage;
  className: string;
}) {
  const { theme } = useTheme();

  return (
    <Highlight
      code={code}
      language={language}
      theme={theme === 'dark' ? themes.vsDark : themes.github}
    >
      {({
        className: prismClassName,
        getLineProps,
        getTokenProps,
        style,
        tokens,
      }) => (
        <pre
          className={`${prismClassName} ${className}`}
          style={{ ...style, background: 'transparent' }}
        >
          {tokens.map((line, lineIndex) => (
            <div {...getLineProps({ line })} key={`line-${lineIndex}`}>
              {line.map((token, tokenIndex) => (
                <span
                  {...getTokenProps({ token })}
                  key={`token-${tokenIndex}-${token.content}`}
                />
              ))}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}

export function MarkdownPreview({
  body,
  className = '',
}: {
  body: string;
  className?: string;
}) {
  return (
    <div
      className={`text-[13px] leading-relaxed text-[var(--color-text-secondary)] ${className}`}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => (
            <a
              className="text-[var(--color-info)] underline underline-offset-2"
              href={href}
              rel="noreferrer"
              target="_blank"
            >
              {children}
            </a>
          ),
          code: ({ children }) => (
            <code className="rounded bg-ops-surface-raised px-1 py-0.5 font-mono text-[12px]">
              {children}
            </code>
          ),
          h1: ({ children }) => (
            <h1 className="mb-2 text-lg font-semibold text-[var(--color-text-primary)]">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-2 text-base font-semibold text-[var(--color-text-primary)]">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-1 text-sm font-semibold text-[var(--color-text-primary)]">
              {children}
            </h3>
          ),
          li: ({ children }) => <li className="ml-5 list-disc">{children}</li>,
          ol: ({ children }) => <ol className="my-2">{children}</ol>,
          p: ({ children }) => (
            <p className="my-2 first:mt-0 last:mb-0">{children}</p>
          ),
          pre: ({ children }) => (
            <pre className="my-2 overflow-auto rounded bg-ops-surface-raised p-2 font-mono text-[12px]">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="my-2 overflow-auto">
              <table className="w-full border-collapse text-left text-xs">
                {children}
              </table>
            </div>
          ),
          td: ({ children }) => (
            <td className="border border-ops-border px-2 py-1">{children}</td>
          ),
          th: ({ children }) => (
            <th className="border border-ops-border bg-ops-surface-raised px-2 py-1 font-medium text-[var(--color-text-primary)]">
              {children}
            </th>
          ),
          ul: ({ children }) => <ul className="my-2">{children}</ul>,
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
