# MCP Servers

MCP servers are how a workflow's agent reaches read/write capability against
external systems, plus the platform's own communication and memory.

Servers are grouped by whether the platform needs them to operate:

- **`core/`** — required for baseline functionality and enabled for every
  instance: `message`, `memory`, `knowledge`, `platform`.
- **`integrations/`** — optional, external-system servers a workflow opts into:
  `salesforce`, `jira`, `splunk`, `cloudwatch`, `github`.

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

### `knowledge` — immutable source grounding

No workflow-supplied credentials or repository URLs are accepted. The workflow
must send `X-Task-Workflow`; the server exposes only promoted, locally hydrated
Knowledge Sources when that workflow declares the `knowledge` server in its own
`.mcp.json`. Sources remain unavailable until their initial immutable version is
successfully indexed.

| Tool | Purpose |
| --- | --- |
| `list_sources` | List authorized, locally ready source aliases. |
| `search_source` | Search literal text in an authorized immutable source snapshot and return bounded path/line previews. |
| `search_symbols` | Search one authorized immutable source version. |
| `get_symbol` | Read one source symbol and provenance. |
| `get_neighbors` | Inspect bounded graph relationships. |
| `find_paths` | Find bounded symbol paths in one source graph. |
| `get_source_excerpt` | Read a bounded, commit-pinned source excerpt. |

### `platform` — platform self-service

No `mcps.config` fields. `propose_skill_update` uses `WORKFLOW_REPO_URL` and
the App selected by `github.workflow_repo_connection`, derives the GitHub API
origin and repository from that connection, and opens a PR only there. It accepts only
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
| `create_bug_ticket` | Create a Jira Bug issue through API v3. |

### `splunk`

Read-only. Auth via `Authorization: Bearer`, `x-splunk-token`, or
`x-splunk-username` + `x-splunk-password`. Username/password is exchanged for
`Authorization: Splunk` by default; set `mcps.config.splunk.session_cookie_name`
when the deployment instead requires the session key in a named cookie.
Credentials and `x-splunk-base-url` are supplied by each workflow's MCP request
headers. `search_logs` accepts text or SPL and defaults to the last 15 minutes
when no time window is supplied. Configure `max_results` and `max_query_chars`
under `mcps.config.splunk`;
`max_window_hours` defaults to 24. `get_search_results` accepts a Splunk search
job dispatch ID (`sid`) and retrieves its metadata and finalized results in one
tool call. `max_evidence_bytes` defaults to 65536 and bounds the complete
compact response.

| Tool | Purpose |
| --- | --- |
| `search_logs` | Search with text or SPL over an optional bounded window and return compact redacted evidence. |
| `get_search_results` | Retrieve metadata and compact results for one finalized Splunk search job `sid`. |

### `cloudwatch`

Read-only. Requires `x-aws-region`, `x-aws-account-id`,
`x-aws-access-key-id`, and `x-aws-secret-access-key` request headers. Temporary
credentials may additionally provide `x-aws-session-token`. Region and account are allowlisted under
`mcps.config.cloudwatch` using `allowed_regions` and `allowed_account_ids`.
Configure exact `allowed_log_groups`, optional `allowed_log_group_prefixes`,
optional `alarm_log_group_mappings`, and optional
`alarm_dimension_log_group_templates` under `mcps.config.cloudwatch`. Exact
alarm mappings take precedence. For unmapped metric alarms, dimension templates
may derive allowlisted log groups from trusted dimensions such as
`FunctionName: [/aws/lambda/{value}]` or
`ApiName: [/aws/apigateway/{value}]`; the MCP never guesses from an alarm name.
Deployment-specific names and mappings belong in the workflow repository;
the public MCP contains no production resource names. Configure
`max_results`, `max_query_chars`, and `max_window_hours`; `max_scan_bytes`
defaults to 536870912 and rejects completed
queries whose provider-reported scan exceeds that ceiling or omits scan
statistics. `max_evidence_bytes` defaults to 65536. Requests fail closed without
policy. The MCP constructs an isolated AWS SDK (`boto3`) session from each
workflow request. Times accept bounded ISO 8601 or relative values such as
`-1h`.

| Tool | Purpose |
| --- | --- |
| `search_logs` | Run a bounded CloudWatch Logs Insights query and return compact occurrence summaries. |
| `get_log_events` | Return compact evidence from one exact log stream. |
| `describe_log_groups` | Discover candidate log groups before a targeted query. |
| `get_alarm_log_groups` | Resolve a validated alarm ARN through an exact mapping or configured metric-dimension template. |
| `describe_alarm` | Retrieve projected current state and metric identity for a validated alarm ARN. |
| `get_alarm_history` | Retrieve bounded state transitions for a validated alarm ARN. |

### `github`

Supports read-only repository investigation plus explicitly enabled issue
creation and updates on GitHub.com and allowlisted GitHub Enterprise origins.
Platform config owns only host and response bounds: `allowed_hosts`,
`max_results` (maximum 50), `max_excerpt_lines` (maximum 500), and
`max_file_bytes` (maximum 5 MiB) under `mcps.config.github`. Requests fail
closed when no host allowlist is configured.

Each workflow supplies `x-github-repositories`, a bounded JSON map from stable
repository aliases to a named platform GitHub connection, owner, repository,
allowed paths, and optional `issue_write` boolean. The server mints its own
short-lived installation token. Repository identity and path/write scope remain
workflow-owned policy and are never tool arguments; App keys remain platform
secrets.

For example, a workflow can pass this JSON through its `.mcp.json`
`X-GitHub-Repositories` header while the named connection definitions and keys
remain in platform config:

```json
{
  "public-docs": {
    "connection": "github-public",
    "owner": "example",
    "repo": "public-docs",
    "allowed_paths": ["docs"],
    "issue_write": false
  },
  "online": {
    "connection": "github-enterprise",
    "owner": "onlinefirst",
    "repo": "online",
    "allowed_paths": ["src", "deploy"],
    "issue_write": true
  }
}
```

Source excerpts require an exact 40-character commit SHA. Issue and pull
request searches are separate so issue-only matches are not conflated with PR
merge state. A zero result means only that the submitted bounded query found
no match.

| Tool | Purpose |
| --- | --- |
| `get_repository` | Return bounded metadata for one configured repository alias. |
| `resolve_commit` | Resolve a branch, tag, or commit ref to an immutable SHA. |
| `search_code` | Return bounded path candidates within the repository path allowlist. |
| `get_file_excerpt` | Return a bounded UTF-8 excerpt at an exact commit SHA. |
| `search_issues` | Search issues only by bounded query, state, and optional labels. |
| `search_pull_requests` | Search PRs separately and return authoritative merge status. |
| `create_issue` | Create a bounded issue when the repository alias sets `issue_write: true`. |
| `update_issue` | Update title, body, state, or labels when the alias sets `issue_write: true`. |

### Shared evidence preprocessing

Splunk and CloudWatch parse structured fields, normalize volatile identifiers,
group equivalent messages, report occurrence counts and first/last timestamps,
retain small samples, truncate stacks, and enforce a serialized response budget
before evidence reaches the model. Shared built-in
redaction covers email addresses, UUIDs, 12-digit account values, common ID
labels, Finnish personal identity codes, and grouped 16-digit payment-card
values. Add deployment-specific trusted regexes under
`mcps.config.evidence.redaction_patterns`:

```yaml
mcps:
  config:
    evidence:
      redaction_patterns:
        - pattern: 'customer-[A-Z0-9]+'
          replacement: '<customer>'
```

Correlation remains provider/query specific: skills should select known
request, session, invocation, or trace fields and use bounded `stats`/`dedup`
queries. The platform does not attempt a generic cross-log correlation engine.

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
