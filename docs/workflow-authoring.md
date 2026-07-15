# Workflow Authoring

A **workflow** is a self-contained package the platform assembles into a
runnable bundle and hands to an ephemeral runtime container. Start from
[examples/workflow-repo/workflows/example-workflow/](../examples/workflow-repo/workflows/example-workflow/)
and read its own `README.md` for a guided walkthrough; this document is the
schema/assembly reference.

## Package layout

```
workflows/<name>/
├── agent.yaml       # platform-level config: session, secrets, runtime, schedules
├── settings.json    # Claude Code native settings: permissions, sandbox
├── .mcp.json        # which MCP servers this workflow uses, and their auth headers
├── README.md        # optional, workflow-specific notes
├── CLAUDE.md         # optional, workflow-specific instructions (adds to shared CLAUDE.md)
├── agents/          # optional delegated subagent .md files
├── skills/          # optional SKILL.md reference packages
└── hooks/           # optional hook registration + executables
```

## `agent.yaml`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string, required | Kebab-case identifier, must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$`. |
| `description` | string, required | Human-readable purpose. |
| `version` | string | Semantic version (`\d+\.\d+\.\d+`). |
| `session.max_turns` | int, default `50` | Turn ceiling before a forced stop (1–200). |
| `session.model` | string | A `model_profiles` name from `platform-config.yaml`, or a raw model name. |
| `env` | object | Non-secret `${VAR}` values available for expansion in `.mcp.json`. |
| `secrets.<NAME>.encrypted` | string, required | `ENC[age,<base64>]` — age-encrypted, decrypted at container launch. |
| `secrets.<NAME>.description` | string | Optional label. |
| `runtime.parallel_workers` | int, default `3` | Concurrent sessions for this workflow (1–10). |
| `runtime.heartbeat_interval_sec` | int, default `30` | Heartbeat frequency (10–120). |
| `runtime.lost_task_timeout_sec` | int, default `300` | Heartbeat-loss timeout before marking a task lost (60–3600). |
| `runtime.runtime_timeout_sec` | int, default `300` | Wall-clock ceiling per session attempt (60–7200). |
| `runtime.ask_user_question_reminder` | object | `enabled`, `min_turns`, `turn_budget_ratio`, `time_budget_ratio`, `recent_question_turn_window` — nudges the agent to ask before burning its turn/time budget. |
| `runtime.alert_coalesce_window_sec` | int, default `300` | Default connector alert-dedup window. |
| `runtime.container_image` | string | Override the runtime image for this workflow. |
| `runtime.memory_volumes` | list[string] | Named memory volumes for this workflow. |
| `schedules[]` | array | `name`, `cron` (5-field), `prompt`, optional `message_channel`, `enabled` (default `true`). |
| `messaging.channels` | list[string] | Message-bus channels this workflow listens on. |
| `messaging.trigger_pattern` | string | Optional trigger pattern (e.g. `@agent`). |

`agent.yaml` is validated against
[schemas/agent-yaml-schema.json](../schemas/agent-yaml-schema.json) —
the gateway's provisioner runs this validation automatically against every
discovered workflow, on every startup and every workflow-repo sync, for every
instance. Malformed `agent.yaml` files are logged as validation warnings; a
workflow repo doesn't need to ship or wire up the schema itself.

Platform-wide secrets (message bus tokens, etc.) belong in
`platform-config.yaml`'s `secrets:`, not here — `agent.yaml` secrets are for
this workflow's own external-system credentials.

## `settings.json`

Native Claude Code settings: `permissions.deny` (glob patterns for blocked
tools), `sandbox.filesystem.allowWrite`/`denyRead`, and
`sandbox.credentials.envVars` (the runtime auto-populates this deny-list from
`agent.yaml` + `platform-config.yaml` secrets — see
[Security](security.md#sandbox-credential-deny-list)). Author explicit
allow-list entries here only for cases that must bypass the automatic deny.

## `.mcp.json`

```json
{
  "mcpServers": {
    "salesforce": {
      "type": "http",
      "url": "http://mcp-salesforce:8100/mcp",
      "headers": {
        "Authorization": "Bearer ${SALESFORCE_TOKEN}",
        "X-Salesforce-Instance-Url": "${SALESFORCE_INSTANCE_URL}"
      }
    }
  }
}
```

Each key is a server id (`message`, `memory`, `platform`, `salesforce`,
`jira`, `splunk`, `cloudwatch`, ...; see [MCP servers](mcps.md)). `${VAR}`
header values are expanded from `agent.yaml` `env`/`secrets`, workflow-level
env, platform `runtime_env`, and task-injection headers (`TASK_ID`,
`TASK_WORKFLOW`, `MESSAGE_CHANNEL`, `MESSAGE_CHANNEL_ID`,
`MESSAGE_THREAD_ID`).

## Skills and hooks

- **Skills** (`skills/<name>/SKILL.md`) are markdown knowledge packages an
  agent invokes via the `Skill` tool. The platform ships shared skills; a
  workflow can add its own, and a workflow-local skill wins over a shared one
  with the same name. Domain knowledge belongs in skills (or agent prompts, or
  Hindsight memory), not hard-coded into instructions.
- **Hooks** (`hooks/hooks.json`) register Claude Code lifecycle hooks for
  workflow-specific side effects. Shared hook executables (e.g. the Hindsight
  auto-recall/auto-retain hooks — see [Memory](memory.md)) are injected into
  the staged workspace at runtime; a workflow only owns the `hooks.json`
  registrations. Reference an injected hook with a relative command such as
  `./hooks/auto_recall_hook.py` since Claude runs from the staged workflow
  root. Keep programmatic side effects in hooks and multi-step reasoning in
  the skill or agent the hook invokes.

## Bundle assembly

`shared/lib/workflow_bundles.py::build_workflow_bundle` merges three layers,
each later layer overwriting (shadowing) the same path in the previous one:

1. **Platform core** — this repo's `CLAUDE.md`, `skills/`, `hooks/`.
2. **Workflow repo shared assets** — the workflow repo's own `CLAUDE.md`,
   `skills/`, `hooks/`.
3. **Workflow-local** — `workflows/<name>/{agent.yaml, settings.json,
   .mcp.json, README.md, CLAUDE.md, agents/, skills/, hooks/}`.

`CLAUDE.md` shadowing is a **whole-file replace, not a merge** — it's a plain
file copy (`shutil.copy2`), so a workflow repo's own root `CLAUDE.md` (layer 2)
entirely replaces the platform's, and a workflow-local `CLAUDE.md` (layer 3)
would replace both. If you want your own instructions *in addition to* the
platform's shared baseline, copy the platform's `CLAUDE.md` content into your
own and add to it — the example repo intentionally ships no root `CLAUDE.md`
so the platform's baseline applies unless you choose to override it. `skills/`
and `hooks/` are directories, so they overlay per-file instead: a same-named
file in a later layer replaces just that file, and everything else merges in.

Bundle build also scans every YAML/JSON file for plaintext-looking secrets
(see [Security](security.md#bundle-integrity)) and fails the build rather than
producing a bundle with a leaked value.

The output is a bundle directory plus `manifest.yaml`:

```yaml
bundle_version: 1
platform_version: "x.y.z"          # from this repo's pyproject.toml
workflow:
  name: my-workflow
  config_hash: "sha256:..."
repo:
  name: local
  url: ""
  ref: ""
  commit: ""
assets:
  core_sha: "sha256:..."
  team_sha: "sha256:..."
  workflow_sha: "sha256:..."
  sources:
    skills/shared-triage/SKILL.md: team
    skills/reflect/SKILL.md: core
    agents/coordinator.md: workflow
  shadowed:
    - {path: "skills/foo/SKILL.md", by: "workflow"}
created_at: "2025-01-15T10:30:00Z"
```

`platform_version` is what [the compatibility check](deployment.md#compatibility-policy)
compares against the running platform at sync time.

`assets.sources` records the final owner of each merged file: `core` for the
public platform, `team` for workflow-repo shared content, and `workflow` for
workflow-local content. Reflection uses this map to consider only `team` and
`workflow` instructions/skills; it never evaluates public `core` skills for
optimization.

## Building a bundle manually

```sh
python scripts/build_workflow_bundle.py my-workflow \
  --workflow-root /path/to/workflow-repo \
  --output-dir dist/bundles \
  --repo-name my-org/my-workflow-repo \
  --repo-url https://github.com/my-org/my-workflow-repo \
  --repo-ref main \
  --repo-commit "$(git rev-parse HEAD)"
```

In normal operation you don't need this — the sync pipeline
(`make bootstrap`'s first sync, or the control-plane UI's **Sync now**) builds
every discovered workflow's bundle automatically. This is mainly useful for
inspecting a bundle locally or debugging assembly/shadowing.
