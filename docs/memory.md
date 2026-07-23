# Memory and Learning

Agents keep three distinct kinds of memory. They are separate systems with
separate lifecycles — don't conflate them.

| Tier | What it is | Lifetime | Backed by |
| --- | --- | --- | --- |
| Long-term memory | Cross-session incident knowledge and retrieval | Persists across all sessions | Hindsight service |
| Per-agent project memory | Claude Code's own project-memory files | Persists per workflow across sessions | Memory volume + object-storage backup |
| Prompt/context | `CLAUDE.md`, agents, skills | Changes only via repo edits | The bundle |

## Long-term memory (Hindsight)

Workflows retain findings, recall similar past incidents, and reflect on
patterns through the `memory` MCP server (see [MCP servers](mcps.md)). Two
shipped hooks make the common cases automatic so agents don't have to
remember to call the tools:

| Hook | Event | Behavior |
| --- | --- | --- |
| `hooks/auto_recall_hook.py` | `SessionStart` | Queries long-term memory for entries relevant to the coordinator task prompt and injects them as invisible `additionalContext` (labeled `[Long-term memory — similar past incidents]`), so the main agent starts with that context without an explicit `recall_similar` call. It skips subagents. |
| `hooks/retain_incident_hook.py` | `SubagentStop` | On investigator completion, writes two memories: a business-facing RCA record (for recall and digests) and a workflow-learning trace (for later reflection). Never blocks the session. |

Explicit tools remain available for targeted retrieval — e.g. a digest
workflow calls `recall_for_digest` over a recent window, and the weekly
reflection run calls `reflect_patterns`.

### Weekly reflection

The [`skills/reflect`](../skills/reflect/SKILL.md) skill drives a scheduled
weekly run: it calls `reflect_patterns` on the workflow-learning bank over the
last 7 days, looks for recurring investigation problems (tool loops, wrong or
weak parameters, missing/stale skills, signals that reliably led to
resolution), and — when warranted — proposes durable skill/instruction updates
as a GitHub PR through the `platform` MCP. It is deliberately about *workflow
behavior*, not business incident themes.

### Memory banks

Each workflow routes to its own Hindsight banks so knowledge stays isolated.
Bank routing is configured in `platform-config.yaml` under `memory.banks`,
per kind (`business` and `learning`):

```yaml
memory:
  backend: hindsight
  banks:
    business:
      incident-investigator: incident-rca-customer
      sf-alerts-investigator: incident-rca-sf-alerts
    learning:
      incident-investigator: workflow-learning-customer
      sf-alerts-investigator: workflow-learning-sf-alerts
```

The `memory` MCP (backed by Hindsight) resolves the right bank server-side
from the `X-Task-Workflow` request header; a workflow without a mapping falls
back to the `incident-rca` (business) and `workflow-learning` (learning)
defaults.

`memory.backend: hindsight` identifies the only supported long-term-memory
backend today. It does not replace `HINDSIGHT_URL`: gateway health/API calls,
the session manager's runtime environment, hooks, and `mcp-memory` all use
that URL to reach the Hindsight service.

## Per-agent project memory

This is Claude Code's native project memory, persisted across ephemeral
sessions:

- At runtime it appears as ordinary files under the workspace's project-memory
  directory; agents discover and update it with standard file tools (`Glob`,
  `Grep`, `Read`, `Write`) rather than a special memory tool. `MEMORY.md` is a
  convention, not a magic file — other note files can live beside it.
- session-manager backs project memory up to object storage after each session
  (`agent-memory/{agent}/latest.tar.gz` plus a timestamped version) and
  restores it before the next session. Compose uses a named Docker volume as a
  cache; Kubernetes Jobs use a task-local `emptyDir` at `/memory`, restored by
  an init helper and uploaded by a completion helper, so no Kubernetes memory
  PVC is required.
- `MEMORY_SYNC_MODE` controls the local service path (`docker_volume` or
  `filesystem`). Kubernetes runtime Jobs always use the object-store helper
  flow described above — see [Configuration](configuration.md).

Housekeeping prunes old agent-memory versions and expired learning-memory per
the retention settings in [Configuration](configuration.md), while leaving
business RCA memory intact.

## Prompt/context

`CLAUDE.md`, `agents/`, and `skills/` are curated, repo-authored knowledge
merged into the bundle — not memory-volume state. They change only through
repository edits or PRs (including the reflection PRs above). See
[Workflow authoring](workflow-authoring.md) for how these are assembled and
how shared vs. workflow-local assets take precedence.
