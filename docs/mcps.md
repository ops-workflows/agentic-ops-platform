# MCP Servers

MCP servers are how a workflow's agent reaches read/write capability against
external systems, plus the platform's own communication and memory.

Servers are grouped by whether the platform needs them to operate:

- **`core/`** — required for baseline functionality and enabled for every
  instance: `message`, `memory`, `platform`.
- **`integrations/`** — optional, external-system servers a workflow opts into:
  `salesforce`, `jira`, `splunk`, `cloudwatch`.

A deployment turns servers on in `platform-config.yaml`, and a workflow
references the ones it uses in its `.mcp.json`:

```yaml
mcps:
  enabled:
    - message        # core
    - memory         # core
    - platform       # core
    - salesforce     # optional integration
  config:
    salesforce:
      api_version: v60.0
      # ...per-server policy, see below
```

Every server is an HTTP MCP endpoint; auth and instance context (Salesforce
instance URL, Splunk base URL, AWS region, ...) travel as request headers set
in the workflow's `.mcp.json`, expanded from `${VAR}` at bundle/runtime time —
not from `mcps.config`. `mcps.config.<server_id>` in `platform-config.yaml`
is for **policy**, not per-request credentials.

## Core servers (`mcps/core/`)

### `message` — human communication

No `mcps.config` fields. Resolves channel/thread/team from request headers
injected by the runtime (`x-message-channel`, `x-message-channel-id`,
`x-message-thread-id`, `x-message-team-id`, `x-message-team-name`,
`x-task-id`).

| Tool | Purpose |
| --- | --- |
| `post_message` | Post a markdown message to a channel/thread. |
| `handoff_task` | Post a visible handoff message and enqueue a task for another workflow. |
| `post_rca_summary` | Post a structured root-cause-analysis summary. |
| `ask_approval` | Post an approval question with decision options. |

### `memory` — long-term memory (backed by Hindsight)

No `mcps.config` fields. Bank selection comes from `platform-config.yaml`'s
`memory.banks` (per-workflow, per-kind); falls back to `BANK_INCIDENT_RCA` /
`BANK_WORKFLOW_LEARNING` defaults (`shared/lib/memory_catalog.py`). Backed by
the [Hindsight](https://hindsight.vectorize.io) service today (`HINDSIGHT_URL`);
the server id is `memory` so the backend can change without a workflow-facing
rename.

| Tool | Purpose |
| --- | --- |
| `retain_incident` | Store a durable RCA or learning note. |
| `recall_similar` | Semantic recall of similar past incidents. |
| `reflect_patterns` | Synthesize patterns across stored incidents. |
| `recall_for_digest` | Recall recent raw incidents for a digest. |

### `platform` — platform self-service

No `mcps.config` fields. `propose_skill_update` reuses the bootstrap
`WORKFLOW_REPO_URL` and `WORKFLOW_REPO_PAT`, derives the GitHub repository from
that URL, and opens a PR only in that workflow repository. It accepts only
workflow-local instructions/skills and workflow-repo shared `skills/*/SKILL.md`;
platform-core files are read-only.

| Tool | Purpose |
| --- | --- |
| `create_workflow_task` | Enqueue a follow-up task in another workflow (internal handoff). |
| `propose_skill_update` | Open a GitHub PR proposing a new/updated skill or instructions file. |

## Integration servers (`mcps/integrations/`)

### `salesforce`

Read-only. Config lives under `mcps.config.salesforce`:

| Field | Default | Purpose |
| --- | --- | --- |
| `api_version` | `v60.0` | Salesforce REST API version. |
| `max_query_limit` | `200` | Max records any query tool can return. |
| `max_query_fields` | `10` | Max fields any query tool can request. |
| `allowed_objects` | `[]` | Allowlist for `query_records`/`get_record`/`find_record`/`get_case_comments`. |
| `allowed_tooling_objects` | `[]` | Allowlist for `query_tooling_records` (e.g. `ApexClass`, `ValidationRule`). |
| `filter_required_objects` | `[]` | Objects that must have a filter to query (blocks unfiltered table scans). |
| `object_fields` | `{}` | Default field list per object, used when a tool call omits `fields`. |
| `tooling_object_fields` | `{}` | Default field list per tooling object. |

Auth/context travels via `Authorization: Bearer <token>` and
`x-salesforce-instance-url` headers, not `mcps.config`.

| Tool | Purpose |
| --- | --- |
| `resolve_record_reference` | Resolve an ambiguous id/name into a record id. |
| `describe_object` | Object metadata (fields, permissions, record types). |
| `describe_field` | Single field's metadata. |
| `list_object_fields` | List queryable fields for an object. |
| `find_validation_rules` | Find validation rules matching an error message. |
| `get_record` | Fetch one record by id. |
| `get_case_comments` | Fetch a Case's comments. |
| `query_records` | Filtered/sorted SOQL query. |
| `find_record` | Free-text search across records. |
| `query_tooling_records` | Query Tooling API metadata objects. |

### `jira`

No `mcps.config` fields. Auth/context via `x-jira-base-url`, `x-jira-project`,
`Authorization: Bearer <token>` headers.

| Tool | Purpose |
| --- | --- |
| `create_bug_ticket` | Create a Jira Bug issue (tries API v3, falls back to v2). |

### `splunk`

Read-only. No `mcps.config` fields. Auth via either `x-splunk-token`
(bearer) or `x-splunk-username` + `x-splunk-password` (exchanged for a
session cookie). Requires `x-splunk-base-url`.

| Tool | Purpose |
| --- | --- |
| `search_logs` | Run a bounded SPL search. |
| `get_saved_search` | Inspect a known saved search's state/output. |
| `get_alert_events` | Recent fired-alert entries for a Splunk alert. |

### `cloudwatch`

Read-only. No `mcps.config` fields. Requires `x-aws-region`; optional
`x-aws-account-id` for validation. Uses AWS SDK (`boto3`) credentials
available to the container. Times accept ISO 8601 or relative (`-1h`, `-7d`).

| Tool | Purpose |
| --- | --- |
| `search_logs` | Run a CloudWatch Logs Insights query. |
| `get_log_events` | Fetch raw events from one log stream. |
| `describe_log_groups` | Discover candidate log groups before a targeted query. |

## Shared helpers (`mcps/common.py`)

- `bootstrap_platform_env()` — loads `platform-config.yaml` config + decrypted
  secrets into the process environment at server startup.
- `get_env(name, default="")` — plain env lookup.
- `extract_bearer_token(headers)` — parses `Authorization: Bearer <token>`.
- `require_header(headers, name, description)` — raises if a required header
  is missing.
- `validate_base_url(value, header_name)` — validates an HTTP(S) origin with
  no path/query/fragment.

## Adding a new server

Drop `mcp_<name>.py` into `core/` (if every workflow needs it) or
`integrations/` (if it's opt-in) as an HTTP MCP app, wire a service entry for
it in `deploy/`, add it to `mcps.enabled` in `platform-config.yaml`, and
reference it from a workflow's `.mcp.json` (see
[Workflow authoring](workflow-authoring.md)). Keep servers stateless —
authenticate each request from caller-supplied headers — so they stay
reusable across workflows.

Third-party SaaS MCP servers can be referenced directly by URL from a
workflow's `.mcp.json` without adding anything here:

```json
{
  "mcpServers": {
    "sentry": { "type": "http", "url": "https://mcp.sentry.dev/mcp" }
  }
}
```
