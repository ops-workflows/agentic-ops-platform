'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';

interface ParsedAgentDefinition {
  path: string;
  name: string;
  description: string;
  tools: string[];
  mcpServers: string[];
}

interface PluginPreviewProps {
  agentYaml: string;
  files: Record<string, string>;
  emptyMessage?: string;
  showFiles?: boolean;
}

interface ConfiguredHook {
  label: string;
  scriptName: string;
}

function stripQuotes(value: string): string {
  return value.trim().replace(/^['"]|['"]$/g, '');
}

function readYamlScalar(yaml: string, key: string): string {
  const match = yaml.match(new RegExp(`^${key}:\\s*(.+)$`, 'm'));
  return match ? stripQuotes(match[1]) : '';
}

function parseFrontmatter(markdown: string): Record<string, string | string[]> {
  if (!markdown.startsWith('---')) return {};

  const lines = markdown.split('\n');
  if (lines.length < 3) return {};

  const endIndex = lines.slice(1).findIndex((line) => line.trim() === '---');
  if (endIndex === -1) return {};

  const frontmatterLines = lines.slice(1, endIndex + 1);
  const result: Record<string, string | string[]> = {};
  let currentArrayKey = '';

  for (const rawLine of frontmatterLines) {
    const line = rawLine.replace(/\r$/, '');
    const scalarMatch = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
    if (scalarMatch) {
      const [, key, value] = scalarMatch;
      if (!value.trim()) {
        result[key] = [];
        currentArrayKey = key;
      } else {
        result[key] = stripQuotes(value);
        currentArrayKey = '';
      }
      continue;
    }

    const arrayMatch = line.match(/^\s*-\s+(.*)$/);
    if (arrayMatch && currentArrayKey) {
      const current = result[currentArrayKey];
      const array = Array.isArray(current) ? current : [];
      array.push(stripQuotes(arrayMatch[1]));
      result[currentArrayKey] = array;
      continue;
    }

    currentArrayKey = '';
  }

  return result;
}

function parseAgentDefinitions(
  files: Record<string, string>,
): ParsedAgentDefinition[] {
  return Object.entries(files)
    .filter(
      ([path]) =>
        (path.startsWith('.claude/agents/') || path.startsWith('agents/')) &&
        path.endsWith('.md'),
    )
    .map(([path, content]) => {
      const frontmatter = parseFrontmatter(content);
      return {
        path,
        name:
          typeof frontmatter.name === 'string'
            ? frontmatter.name
            : path.replace(/^(\.claude\/agents\/|agents\/)/, ''),
        description:
          typeof frontmatter.description === 'string'
            ? frontmatter.description
            : '',
        tools: Array.isArray(frontmatter.tools) ? frontmatter.tools : [],
        mcpServers: Array.isArray(frontmatter.mcpServers)
          ? frontmatter.mcpServers
          : [],
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function parseMcpServers(files: Record<string, string>): string[] {
  const mcpJson = files['.mcp.json'];
  if (!mcpJson) return [];
  try {
    const parsed = JSON.parse(mcpJson) as {
      mcpServers?: Record<string, unknown>;
    };
    return Object.keys(parsed.mcpServers || {}).sort();
  } catch {
    return [];
  }
}

function parseSkillNames(files: Record<string, string>): string[] {
  return Object.keys(files)
    .map(
      (path) =>
        path.match(/^(?:\.claude\/skills|skills)\/([^/]+)\/SKILL\.md$/)?.[1] ||
        '',
    )
    .filter(Boolean)
    .sort();
}

function parseHookFiles(files: Record<string, string>): string[] {
  return Object.keys(files)
    .filter(
      (path) =>
        path.startsWith('hooks/') &&
        path.endsWith('.py') &&
        path !== 'hooks/__init__.py',
    )
    .map((path) => path.split('/').pop() || path)
    .sort();
}

function parseConfiguredHooks(files: Record<string, string>): ConfiguredHook[] {
  const hookJson = files['hooks/hooks.json'];
  if (!hookJson) return [];

  try {
    const parsed = JSON.parse(hookJson) as {
      hooks?: Record<
        string,
        Array<{
          matcher?: string;
          hooks?: Array<{ type?: string; command?: string }>;
        }>
      >;
    };
    const configured: ConfiguredHook[] = [];

    for (const [eventName, entries] of Object.entries(parsed.hooks || {})) {
      for (const entry of entries || []) {
        for (const hook of entry.hooks || []) {
          if (hook.type !== 'command' || !hook.command) continue;
          const commandName = hook.command.split('/').pop() || hook.command;
          const friendlyName = commandName
            .replace(/\.py$/, '')
            .replace(/_hook$/, '')
            .split('_')
            .map((w: string) => w.charAt(0).toUpperCase() + w.slice(1))
            .join(' ');
          configured.push({
            label: `${eventName} → ${friendlyName}`,
            scriptName: commandName,
          });
        }
      }
    }

    return configured.sort((left, right) =>
      left.label.localeCompare(right.label),
    );
  } catch {
    return [];
  }
}

function fileTypeLabel(path: string): string {
  if (path === 'agent.yaml') return 'platform';
  if (path === 'settings.json' || path === '.claude/settings.json')
    return 'claude';
  if (path === '.mcp.json') return 'mcp';
  if (path === 'CLAUDE.md') return 'prompt';
  if (path.startsWith('.claude/agents/') || path.startsWith('agents/'))
    return 'agent';
  if (path.startsWith('.claude/skills/') || path.startsWith('skills/'))
    return 'skill';
  if (path.startsWith('hooks/')) return 'hook';
  return 'file';
}

function mcpDetailsHref(serverId: string): string {
  return `/mcp?server=${encodeURIComponent(serverId)}`;
}

export function PluginPreview({
  agentYaml,
  files,
  emptyMessage = 'No plugin files loaded yet.',
  showFiles = true,
}: PluginPreviewProps) {
  const allFiles = useMemo(() => {
    const merged = { ...files };
    if (agentYaml) merged['agent.yaml'] = agentYaml;
    return merged;
  }, [agentYaml, files]);

  const filePaths = useMemo(() => Object.keys(allFiles).sort(), [allFiles]);
  const [selectedPath, setSelectedPath] = useState('agent.yaml');

  useEffect(() => {
    if (filePaths.length === 0) {
      setSelectedPath('');
      return;
    }
    if (!selectedPath || !allFiles[selectedPath]) {
      setSelectedPath(filePaths[0]);
    }
  }, [allFiles, filePaths, selectedPath]);

  const pluginName = readYamlScalar(agentYaml, 'name') || 'unnamed-agent';
  const description = readYamlScalar(agentYaml, 'description');
  const version = readYamlScalar(agentYaml, 'version');
  const agentDefinitions = useMemo(
    () => parseAgentDefinitions(allFiles),
    [allFiles],
  );
  const mcpServers = useMemo(() => parseMcpServers(allFiles), [allFiles]);
  const skills = useMemo(() => parseSkillNames(allFiles), [allFiles]);
  const hooks = useMemo(() => {
    const configuredHooks = parseConfiguredHooks(allFiles);
    const configuredScriptNames = new Set(
      configuredHooks.map((hook) => hook.scriptName),
    );
    const unconfiguredFiles = parseHookFiles(allFiles)
      .filter((scriptName) => !configuredScriptNames.has(scriptName))
      .map((scriptName) => `script: ${scriptName}`);
    return [
      ...configuredHooks.map((hook) => hook.label),
      ...unconfiguredFiles,
    ].sort();
  }, [allFiles]);

  if (!filePaths.length) {
    return (
      <div className="bg-ops-surface border border-ops-border rounded-card p-6 text-sm text-[var(--color-text-tertiary)]">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="bg-ops-surface border border-ops-border rounded-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-lg font-medium text-[var(--color-text-primary)]">
              {pluginName}
            </h3>
            <p className="text-sm text-[var(--color-text-secondary)] mt-1">
              {description || 'No description in agent.yaml'}
            </p>
          </div>
          <div className="text-xs text-[var(--color-text-tertiary)]">
            {version ? `v${version}` : 'No version set'}
          </div>
        </div>

        <div className="grid grid-cols-4 gap-3 mt-4">
          <SummaryCard
            label="Agents"
            value={String(agentDefinitions.length)}
            tone="blue"
          />
          <SummaryCard
            label="MCP Servers"
            value={String(mcpServers.length)}
            tone="green"
          />
          <SummaryCard
            label="Skills"
            value={String(skills.length)}
            tone="yellow"
          />
          <SummaryCard
            label="Hooks"
            value={String(hooks.length)}
            tone="purple"
          />
        </div>
      </div>

      <div
        className={`grid grid-cols-1 ${showFiles ? 'xl:grid-cols-2' : ''} gap-4`}
      >
        <div className="bg-ops-surface border border-ops-border rounded-card p-5">
          <h4 className="font-medium text-[var(--color-text-primary)] mb-3">
            Plugin Graph
          </h4>
          <div className="space-y-3 text-left">
            <div className="flex items-center gap-3">
              <div className="w-2.5 h-2.5 rounded-full bg-[var(--color-accent)]" />
              <div className="bg-[var(--color-accent-muted)] border border-[var(--color-accent)]/30 rounded-btn px-2.5 py-1.5 text-sm text-[var(--color-text-primary)]">
                <strong>{pluginName}</strong>
              </div>
            </div>

            <div className="ml-6">
              <p className="text-xs text-[var(--color-text-tertiary)] mb-1.5">
                Agent Definitions
              </p>
              <div className="space-y-1.5">
                {agentDefinitions.length > 0 ? (
                  agentDefinitions.map((agent) => (
                    <div
                      key={agent.path}
                      className="bg-ops-surface-raised border border-ops-border rounded-btn p-2.5"
                    >
                      <div className="text-sm font-medium text-[var(--color-text-primary)]">
                        {agent.name}
                      </div>
                      <div className="text-xs text-[var(--color-text-tertiary)] mt-0.5 line-clamp-1">
                        {agent.description || agent.path}
                      </div>
                      {agent.mcpServers.length > 0 && (
                        <div className="text-xs text-[var(--color-text-tertiary)] mt-1">
                          MCP:{' '}
                          {agent.mcpServers.map((server, index) => (
                            <span key={server}>
                              {index > 0 ? ', ' : ''}
                              <Link
                                href={mcpDetailsHref(server)}
                                className="text-[var(--color-text-secondary)] underline-offset-2 hover:underline"
                              >
                                {server}
                              </Link>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))
                ) : (
                  <EmptyInline text="No agents/*.md files found" />
                )}
              </div>
            </div>

            <div className="ml-6 grid grid-cols-1 md:grid-cols-3 gap-3">
              <TagSection
                title="MCP Servers"
                items={mcpServers}
                tone="green"
                empty="No MCP servers"
                hrefBuilder={mcpDetailsHref}
              />
              <TagSection
                title="Skills"
                items={skills}
                tone="yellow"
                empty="No skills"
              />
              <TagSection
                title="Hooks"
                items={hooks}
                tone="purple"
                empty="No hooks"
              />
            </div>
          </div>
        </div>

        {showFiles && (
          <div className="bg-ops-surface border border-ops-border rounded-card overflow-hidden">
            <div className="px-4 py-2.5 border-b border-ops-border flex items-center justify-between">
              <h4 className="font-medium text-[var(--color-text-primary)]">
                Plugin Files
              </h4>
              <span className="text-xs text-[var(--color-text-tertiary)]">
                {filePaths.length} files
              </span>
            </div>
            <div className="flex flex-col sm:grid sm:grid-cols-[180px_1fr] lg:grid-cols-[200px_1fr] min-h-[240px] sm:min-h-[300px] max-h-[400px]">
              <div className="border-b sm:border-b-0 sm:border-r border-ops-border overflow-x-auto sm:overflow-y-auto">
                <div className="flex sm:flex-col">
                  {filePaths.map((path) => (
                    <button
                      key={path}
                      onClick={() => setSelectedPath(path)}
                      className={`flex-shrink-0 sm:w-full text-left px-3 py-2.5 border-r sm:border-r-0 sm:border-b border-ops-border/50 hover:bg-ops-surface-raised transition-colors ${
                        selectedPath === path ? 'bg-ops-surface-raised' : ''
                      }`}
                    >
                      <div className="text-sm text-[var(--color-text-secondary)] break-all whitespace-nowrap sm:whitespace-normal">
                        {path}
                      </div>
                      <div className="text-[10px] uppercase tracking-wide text-[var(--color-text-tertiary)] mt-1 hidden sm:block">
                        {fileTypeLabel(path)}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
              <div className="overflow-auto p-4">
                {selectedPath ? (
                  <>
                    <div className="text-xs text-[var(--color-text-tertiary)] mb-2">
                      {selectedPath}
                    </div>
                    <pre className="text-xs text-[var(--color-text-secondary)] whitespace-pre-wrap break-words font-mono">
                      {allFiles[selectedPath]}
                    </pre>
                  </>
                ) : (
                  <div className="text-sm text-[var(--color-text-tertiary)]">
                    Select a file to preview.
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'blue' | 'green' | 'yellow' | 'purple';
}) {
  const styles = {
    blue: 'bg-[var(--color-info-muted)] border-[var(--color-info)]/20 text-[var(--color-info)]',
    green:
      'bg-[var(--color-success-muted)] border-[var(--color-success)]/20 text-[var(--color-success)]',
    yellow:
      'bg-[var(--color-warning-muted)] border-[var(--color-warning)]/20 text-[var(--color-warning)]',
    purple:
      'bg-[var(--color-accent-muted)] border-[var(--color-accent)]/20 text-[var(--color-accent)]',
  } as const;

  return (
    <div className={`rounded-card border p-3 ${styles[tone]}`}>
      <div className="text-xs uppercase tracking-wide opacity-80">{label}</div>
      <div className="text-xl font-medium mt-1">{value}</div>
    </div>
  );
}

function TagSection({
  title,
  items,
  empty,
  tone,
  hrefBuilder,
}: {
  title: string;
  items: string[];
  empty: string;
  tone: 'green' | 'yellow' | 'purple';
  hrefBuilder?: (item: string) => string;
}) {
  const styles = {
    green:
      'bg-[var(--color-success-muted)] border-[var(--color-success)]/20 text-[var(--color-success)]',
    yellow:
      'bg-[var(--color-warning-muted)] border-[var(--color-warning)]/20 text-[var(--color-warning)]',
    purple:
      'bg-[var(--color-accent-muted)] border-[var(--color-accent)]/20 text-[var(--color-accent)]',
  } as const;

  return (
    <div>
      <p className="text-xs text-[var(--color-text-tertiary)] mb-2">{title}</p>
      <div className="flex flex-wrap gap-2">
        {items.length > 0 ? (
          items.map((item) =>
            hrefBuilder ? (
              <Link
                key={item}
                href={hrefBuilder(item)}
                className={`rounded-full px-2.5 py-1 text-xs border ${styles[tone]} underline-offset-2 hover:underline`}
              >
                {item}
              </Link>
            ) : (
              <span
                key={item}
                className={`rounded-full px-2.5 py-1 text-xs border ${styles[tone]}`}
              >
                {item}
              </span>
            ),
          )
        ) : (
          <EmptyInline text={empty} />
        )}
      </div>
    </div>
  );
}

function EmptyInline({ text }: { text: string }) {
  return (
    <span className="text-xs text-[var(--color-text-tertiary)]">{text}</span>
  );
}
