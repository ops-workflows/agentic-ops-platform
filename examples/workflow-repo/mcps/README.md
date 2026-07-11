# MCP Servers

This directory is where a workflow repo can add MCP server implementations
specific to it. Generic, reusable servers (Message, Memory, Platform,
Salesforce, Splunk, CloudWatch, Jira) live in the public platform repo under
`mcps/` and are configured — not copied — from here.

See [docs/mcps.md](../../../docs/mcps.md) in the platform repo for the
core/integrations model and the full config schema for each shipped server.

## Enabling and configuring servers

```yaml
mcps:
  enabled:
    - message
    - memory
    - platform
    - salesforce        # public generic server, configured below
    - my-custom-mcp      # a server defined in this directory
  config:
    salesforce:
      allowed_objects: [Account, Case, Contact]
      # ... see docs/mcps.md for the full schema
```

`mcps.enabled` controls which servers appear in the platform MCP catalog.
`mcps.config.<server_id>` supplies instance-specific policy to a server (the
public Salesforce server, for example, ships no hardcoded object policy —
it's read from here). Per-request auth and context are injected by the
runtime through each workflow's `.mcp.json` headers, never from the model's
tool-call arguments.

## Adding a custom MCP server

See [custom-mcp-example/](custom-mcp-example/) for a minimal FastMCP skeleton
to copy and adapt:

1. Create `mcps/mcp_<name>.py` here using `fastmcp`, exposing an ASGI `app`
   and a `/health` route (mirror a public server such as
   `mcps/integrations/mcp_jira.py` in the platform repo).
2. Read any instance policy with
   `shared.lib.platform_secrets.load_mcp_server_config(path, "<name>")`.
3. Build and run the server image in your deployment override, exposing it at
   `http://mcp-<name>:<port>/mcp`.
4. Reference it from a workflow's `.mcp.json` and add `<name>` to
   `mcps.enabled` in `platform-config.yaml`.
