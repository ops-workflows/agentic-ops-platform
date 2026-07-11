# How to Create a New Workflow Package

## Quick Start

1. Copy the template into your workflow repo:
   ```bash
   cp -r examples/workflow-repo/workflows/example-workflow workflows/my-new-workflow
   ```

2. Edit `workflows/my-new-workflow/agent.yaml`:
   - Replace all `{PLACEHOLDER}` values
   - Configure workflow-local secrets, schedules, and messaging bindings

3. Configure platform-level credentials such as `MESSAGE_BUS_BOT_TOKEN` in `platform-config.yaml`

4. Edit `.mcp.json` with MCP server URLs, auth headers, and session headers

5. Edit `settings.json` with Claude-native permissions and sandbox rules

6. Write agents:
   - Create `agents/coordinator.md` for the default workflow agent
   - Add more delegated agents under `agents/` as needed

7. Write skills:
   - Create `skills/my-skill/SKILL.md` with reference knowledge

8. Optional: edit `hooks/hooks.json` to register workflow-specific hook events. Shared hook executables are injected at runtime; the workflow only owns the registrations. Use relative command paths such as `./hooks/auto_recall_hook.py`.

9. Test locally:
   ```bash
   # The provisioner will auto-detect the new workflow
   docker compose restart gateway
   ```

10. Open a PR for review and merge

## Workflow Structure

```
workflows/my-new-workflow/
├── agent.yaml              # Platform-only config (secrets, runtime, schedules)
├── .mcp.json               # MCP server configs
├── settings.json           # Claude-native permissions + sandbox
├── agents/
│   ├── coordinator.md      # Optional delegated coordinator agent
│   └── sub-agent.md        # Optional delegated subagent definitions
├── skills/
│   └── my-skill/
│       └── SKILL.md        # Reference knowledge for agents
├── hooks/
│   └── hooks.json          # Optional custom hooks
```

## MCP servers wired in `.mcp.json`

This template's `.mcp.json` wires four servers, and `settings.json`'s
`sandbox.network.allowedDomains` lists the matching hostname for each:

- `message`, `memory`, `platform` — the three core servers every instance
  runs; `platform` is what `mcp__platform__create_workflow_task` (cross-workflow
  handoff) and `propose_skill_update` (skill/instruction PR proposals) come
  from. See [docs/mcps.md](../../../../docs/mcps.md).
- `custom-example` — a wiring **example** pointed at
  [mcps/custom-mcp-example/](../../mcps/custom-mcp-example/) in this example
  repo. It only works once you've actually built and deployed that (or your
  own) custom server; remove the entry if you don't need one.

`agents/coordinator.md`'s frontmatter `mcpServers:` list gates which of these
a given agent can actually call — a server present in `.mcp.json` isn't
automatically usable by every agent. Add `custom-example` there too once your
custom server is live.

## Using the Agent Builder (alternative)

Instead of manually creating files, use the Agent Builder UI:
1. Navigate to `/builder` in the control-plane UI
2. Describe your agent in natural language
3. Refine iteratively
4. Click "Create PR" to generate the full workflow structure

The workflow itself is the top-level entrypoint. `agents/*.md` files are reusable delegated agents, not direct platform channels on their own.

## Runtime Workspace and Memory

At execution time, the platform stages a temporary writable workspace for the session inside the runtime container.

- Your workflow directory stays the authored source of truth in Git.
- Shared runtime files such as `CLAUDE.md`, shared skills, and shared hook executables are added in that temporary workspace.
- Workflow memory is mounted separately and made available to Claude inside the staged workspace.
- Session Manager restores memory from MinIO before a run when needed and backs it up again after the run completes.
