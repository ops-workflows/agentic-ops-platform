# Connectors

Connectors are how external systems get work into the platform. A connector
is a small, generic service that reads from one external source and calls
`shared.lib.task_queue.create_task` to enqueue a task for a workflow.

## Model

- **One image, many instances.** A connector implementation (e.g.
  `gcp-pubsub-connector`) is a generic Docker image with no built-in
  knowledge of any specific subscription, workflow, or prompt. Behavior comes
  entirely from an **instance** — an entry under `connectors.instances` in
  `platform-config.yaml`. A shipped generic connector automatically selects
  its single enabled instance of the matching type. Running the same image
  against two instances (e.g. two subscriptions feeding two workflows) means
  running two containers with different `CONNECTOR_INSTANCE_ID` values, not
  two images.
- **`${VAR}` expansion.** Instance config values can reference
  `platform-config.yaml`'s `config:` values, decrypted `secrets:`, or the
  process environment via `${VAR}` placeholders, expanded at load time
  (`shared/lib/platform_secrets.py`). The connector reads its instance with
  `load_connector_instance(path, instance_id)`.
- **Reference `connector.yaml`.** Each connector directory ships a
  `connector.yaml` as a documentation-only example of its instance schema; it
  is **not** read at runtime. Live config always comes from
  `platform-config.yaml` via `CONNECTOR_INSTANCE_ID`.

## Configuring an instance

Every instance shares four sections; each connector defines what goes inside:

- `source` — where/how to read (subscription, polling endpoint, query, ...).
- `target` — the `workflow` the task goes to, and the message-bus channel
  status updates post to.
- `parsing` — how to extract task metadata from the event/record (dot-path
  field mapping).
- `coalescing` — whether repeated alerts for the same key merge into one task
  within a window (the coalesce key is `{workflow}:{metadata[key_field]}`).

```yaml
connectors:
  enabled:
    - sf-alert-email-intake      # instance ids surfaced in the catalog
  instances:
    sf-alert-email-intake:
      type: gcp-pubsub           # which connector implementation runs it
      display_name: Salesforce Alert Email Intake
      source:
        type: pubsub
        project: ${GCP_PROJECT}
        subscription: ${GCP_PUBSUB_SUBSCRIPTION}
      target:
        workflow: sf-alerts-investigator
      parsing:
        format: json
        extract:
          bucket: bucket
          object: name
      coalescing:
        enabled: true
        window_sec: 300
        key_field: object
```

## Available connectors

### `gcp-pubsub-connector`

Generic GCP Pub/Sub subscriber, with optional fetch of a GCS object the
message references.

```yaml
type: gcp-pubsub
source:
  type: pubsub
  subscription: ${GCP_PUBSUB_SUBSCRIPTION}   # id, or a full projects/.../subscriptions/... path
  project: ${GCP_PROJECT}                     # used to build the path when subscription isn't already full
  gcs_payload:                                # optional: fetch a GCS object referenced by the message
    enabled: true
    bucket_field: bucket                       # dot-path into the decoded JSON payload
    name_field: name
    metadata_key: object_text                  # metadata key the fetched text is stored under
    max_bytes: 200000                          # truncate the object read at this size
    parser: text                               # `text` (default) or `email` for RFC 822/MIME files
    max_body_chars: 4000                       # `email` only: readable body length after MIME/HTML parsing
target:
  workflow: example-workflow
  channel: gcp-pubsub                          # task.channel label
  prompt_template: |                           # rendered via str.format_map over payload + metadata;
    Process this Pub/Sub event.                # missing keys render as empty strings
    {payload_text}
parsing:
  format: json
  extract:                                     # dot-path extraction from the decoded JSON payload
    event_id: id
    event_type: type
  email_extract:                               # optional regexes, used with `gcs_payload.parser: email`
    alert_id: "Alert ID: ([A-Z0-9-]+)"         # capture group 1 is stored as metadata
    description: body                           # use the readable parsed email body directly
coalescing:
  enabled: false
  window_sec: 300
  key_field: event_id
```

Notes: messages are decoded as UTF-8 JSON (non-JSON still creates a task with
`payload_text`); a failed task creation `nack`s the message for redelivery.
For `gcs_payload.parser: email`, the connector extracts `email_subject`,
`email_sender`, `email_recipient`, `email_date`, and `email_body_text`.
`metadata_key` is populated with the readable body, preferring `text/plain` and
falling back to HTML-to-text. This lets a deployment configure email intake
without embedding a provider-specific parser in the connector image.
`parsing.email_extract` can add deployment-specific metadata from regular
expressions over the parsed headers and body; unmatched expressions are stored
as `null`.
The connector never selects an outbound message channel: a threadless task uses
the target workflow's first `messaging.channels` entry, while a message-initiated
task replies in its original thread.
Requires GCP application default credentials reachable by
`google-cloud-pubsub` (and `google-cloud-storage` if `gcs_payload` is used).
On Kubernetes, prefer workload identity. When that is unavailable, put an
age-encrypted `GCP_SERVICE_ACCOUNT_JSON` in `platform-config.yaml`; the
connector decrypts it and exposes it as a temporary ADC credential file.

### `servicenow-connector`

Polls the ServiceNow Table API for matching records.

```yaml
type: servicenow
source:
  type: polling
  interval_sec: 60
  instance_url: ${SERVICENOW_INSTANCE_URL}
  table: incident
  query: "state=1^priority<=3"                 # encoded sysparm_query
  fields:                                      # optional; limits sysparm_fields
    - number
    - priority
    - caller_id
target:
  workflow: incident-investigator
parsing:
  format: json
  extract:                                     # dot-path extraction; supports "field.display_value"
    incident_id: "number"
    severity: "priority"
    service: "cmdb_ci.display_value"
    customer: "caller_id.display_value"
    description: "description"
coalescing:
  enabled: true
  window_sec: 300
  key_field: incident_id
```

Notes: requests use `sysparm_display_value=true` so `extract` can read
`<field>.display_value`. Requires `SERVICENOW_USERNAME` /
`SERVICENOW_PASSWORD` (normally supplied encrypted via `platform-config.yaml`
`secrets:`).

## Adding a new connector implementation

1. Create a directory under `connectors/` with a `Dockerfile` and `main.py`
   (a `connector.yaml` example is conventional but optional).
2. In `main.py`: resolve `CONNECTOR_INSTANCE_ID`, load the instance with
   `load_connector_instance(...)`, read from the source using the instance
   config, and enqueue tasks via `shared.lib.task_queue.create_task()`.
3. Add the service to `deploy/docker-compose.yml` (or a private override) with
   `PLATFORM_CONFIG_FILE` and `CONNECTOR_INSTANCE_ID` in its environment:

   ```yaml
   my-new-connector:
     build:
       context: ..
       dockerfile: connectors/my-new-connector/Dockerfile
     profiles: ["my-new-connector", "connectors"]
     environment:
       PLATFORM_CONFIG_FILE: ${PLATFORM_CONFIG_FILE:-/app/platform-config.yaml}
       CONNECTOR_INSTANCE_ID: ${MY_CONNECTOR_INSTANCE_ID}
     depends_on:
       postgres:
         condition: service_healthy
   ```
