# Example Workflow Repo

This directory mirrors the exact structure a **workflow repo** must have to
work with the Agentic Ops platform. Copy it (or just the pieces you need) to
bootstrap your own workflow repo:

```sh
cp -r examples/workflow-repo /path/to/my-workflow-repo
```

See [docs/workflow-authoring.md](../../docs/workflow-authoring.md) for the
full package/bundle reference, [docs/configuration.md](../../docs/configuration.md)
for the config-layer model, and the platform root
[README.md](../../README.md) for how to point a running platform at your repo.

## What's here

```
examples/workflow-repo/
├── platform-config.example.yaml   # instance config template (mcps, connectors,
│                                   # message bus, memory banks, model profiles)
├── litellm.config.example.yaml    # model-gateway (LiteLLM) config template
├── connectors/
│   ├── README.md                  # how to enable/configure/add a connector
│   └── custom-connector-example/  # minimal skeleton to copy and adapt
├── mcps/
│   ├── README.md                  # how to enable/configure/add an MCP server
│   └── custom-mcp-example/        # minimal FastMCP skeleton to copy and adapt
├── skills/
│   ├── README.md                  # repo-wide shared skills (precedence vs. platform/workflow-local)
│   └── custom-skill-example/      # minimal skeleton to copy and adapt
├── deploy/
│   ├── README.md                  # the "convenience" sibling-checkout deploy mode
│   ├── docker-compose.override.yml
│   └── k8s-values.yaml
└── workflows/
    └── example-workflow/          # copy-and-fill workflow package template
        ├── agent.yaml             # platform-only config: secrets, runtime, schedules
        ├── settings.json          # Claude-native permissions + sandbox
        ├── .mcp.json              # MCP servers this workflow uses
        ├── agents/                # delegated agent prompts
        ├── skills/                # domain-knowledge packages
        └── hooks/                 # hook registrations
```

`workflows/example-workflow/` is a **copy-and-fill template** — its
`agent.yaml`, `.mcp.json`, `settings.json`, and `agents/*.md` still contain
`{PLACEHOLDER}` values (`{WORKFLOW_NAME}`, `{MAIN_AGENT}`, `{ROLE}`, ...). It
is not meant to run as-is; see its own
[README.md](workflows/example-workflow/README.md) for the fill-in steps. Its
`settings.json` demonstrates a `permissions.ask` entry (approval-gated tool
calls) and a populated `sandbox.network.allowedDomains` matching the MCP
servers wired in its `.mcp.json` — customize both for the servers your own
workflow actually uses. Adding integrations (Salesforce, Splunk, connectors,
...) shipped by the platform is documented in
[docs/mcps.md](../../docs/mcps.md) and [docs/connectors.md](../../docs/connectors.md);
adding your own custom MCP server or connector is documented in
[mcps/README.md](mcps/README.md) and [connectors/README.md](connectors/README.md)
in this example.

This example intentionally ships no root `CLAUDE.md`. If you add one to your
own workflow repo, it **replaces** (not merges with) the platform's shared
`CLAUDE.md` in the bundle — see
[Bundle assembly](../../docs/workflow-authoring.md#bundle-assembly).

## `agent.yaml` validation

The platform validates every discovered `agent.yaml` against
[schemas/agent-yaml-schema.json](../../schemas/agent-yaml-schema.json)
automatically — the gateway's provisioner runs this on every startup and every
workflow-repo sync, for every instance. A malformed `agent.yaml` is logged as
a validation warning; nothing in your workflow repo needs to ship or wire up
the schema itself.
