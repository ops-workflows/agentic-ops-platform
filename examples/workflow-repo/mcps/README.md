# MCP Servers

This directory is where a workflow repo can add MCP server implementations
specific to it. Generic, reusable servers (Message, Memory, Platform,
Salesforce, Splunk, CloudWatch, GitHub, Jira) live in the public platform repo under
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
      - my-custom-mcp     # a server defined in this directory
   config:
      salesforce:
         allowed_objects: [Account, Case, Contact]
         # ... see docs/mcps.md for the full schema
      splunk:
         max_window_hours: 24
         max_evidence_bytes: 65536
      evidence:
         redaction_patterns:
            - pattern: 'customer-[A-Z0-9]+'
              replacement: '<customer>'
      cloudwatch:
         allowed_regions: [eu-west-1]
         allowed_account_ids: ['123456789012']
         allowed_log_groups: [/aws/lambda/production-api]
         alarm_log_group_mappings:
            production-apiAlarm: [/aws/lambda/production-api]
         max_window_hours: 24
         max_scan_bytes: 536870912
         max_evidence_bytes: 65536
      github:
         allowed_hosts: [api.github.com, github.company.example]
         max_results: 20
         max_excerpt_lines: 200
         max_file_bytes: 1048576
```

`mcps.enabled` controls which servers appear in the platform MCP catalog.
`mcps.config.<server_id>` supplies instance-specific policy to a server (the
public Salesforce, Splunk, CloudWatch, and GitHub servers ship no instance-specific
resource policy; it is read from here). Splunk and CloudWatch fail closed when
their required host/index or account/region/log-group policy is absent.
GitHub fails closed without an allowed host and workflow-supplied alias,
connection, and path scope. Set `issue_write: true` on only the aliases that
may create or update issues. The MCP resolves short-lived installation tokens
from the named platform GitHub App connection; credentials are never request
headers or model tool-call arguments.

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
