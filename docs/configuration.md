# Configuration

Agentic Ops configuration is split into three layers so the workflow-repo
pointer is never circular (you shouldn't need the repo's own config file to
know how to fetch the repo).

| Layer | Owner | Committed? | Holds |
| --- | --- | --- | --- |
| 1. Bootstrap / infra | Operator | No (generated) | Workflow-repo pointer + PAT, `AGE_IDENTITY`, one `LLM_API_KEY`, and direct-container secrets (Postgres/object storage). |
| 2. Instance config | Workflow repo | Yes (secrets age-encrypted) | `platform-config.yaml`: message bus, MCPs, connectors, memory banks, model profiles, workflow secrets. |
| 3. Workflow packages | Workflow repo | Yes | `workflows/`, shared `skills/`/`hooks/`, custom `mcps/`/`connectors/`. |

Boot sequence: read layer 1 → clone/sync the workflow repo at the pinned ref →
point `PLATFORM_CONFIG_FILE` at the repo's `platform-config.yaml` → load layer
2 → build bundles → run. See [Deployment](deployment.md) for how layer 1 is
generated (`make bootstrap`) and how sync/versioning work.

## Layer 1 — bootstrap / infra settings

These are environment variables read by `shared/lib/config.py`'s `Settings`
class. Env vars always take precedence; unset ones may be overlaid from the
workflow repo's `platform-config.yaml` `config:` section once it has been
fetched (see [Loading order](#loading-order) below) — except
`WORKFLOW_REPO_URL`/`REF`/`PAT`, which are bootstrap-only and never read from
repo-owned config.

### Workflow repository

| Env var | Default | Purpose |
| --- | --- | --- |
| `WORKFLOW_REPO_URL` | `""` | Canonical Git URL of the workflow repo. A remote source syncs it; a local source uses it for GitHub version lookup and reflection PRs while continuing to use its mounted checkout. |
| `WORKFLOW_REPO_REF` | `""` | Git ref (tag/SHA) to sync; the bootstrap default until an operator pins a different ref from the UI. |
| `WORKFLOW_REPO_PAT` | `""` | PAT used to authenticate clone/fetch/version lookup for a private repo and to create reflection PRs in that same repo. Bootstrap-only. |
| `WORKFLOW_REPO_PATHS` | `""` | `os.pathsep`-separated list of mounted workflow roots (local-path source mode). Each entry can be a single workflow dir, a directory of workflows, or a repo root containing `workflows/`. |
| `WORKFLOW_REPO_LOCAL_PATH` | `/workspace/workflows` | Container-side path the workflow repo is synced/mounted to. |
| `REPO_PATH` | `""` | Path to a checked-out/mounted workflow repo root. |
| `HOST_REPO_ROOT` | `""` | Host-side bind-mount path used by the Compose convenience override. |

### Platform config file

| Env var | Default | Purpose |
| --- | --- | --- |
| `PLATFORM_CONFIG_FILE` | `/app/platform-config.yaml` | Path to the instance's `platform-config.yaml`, read after the workflow repo is fetched. |
| `PLATFORM_SECRETS_FILE` | `""` | Deprecated alias for `PLATFORM_CONFIG_FILE`. |

### Database

| Env var | Default | Purpose |
| --- | --- | --- |
| `PG_HOST` | `postgres` | Postgres host. |
| `PG_PORT` | `5432` | Postgres port. |
| `PG_DB` | `agentic_ops` | Database name. |
| `PG_USER` | `agentic_ops` | Database user. |
| `PG_PASSWORD` | `""` | Database password. |

### Object storage

One provider-neutral abstraction (`shared/lib/object_store.py`) backs both
workflow bundles and agent-memory backups across supported deployment targets.

| Env var | Default | Purpose |
| --- | --- | --- |
| `OBJECT_STORE_PROVIDER` | `s3` | `s3` (MinIO, AWS S3, or any S3-compatible endpoint) or `gcs` (Google Cloud Storage). |
| `OBJECT_STORE_ENDPOINT` | `minio:9000` | Endpoint host:port (`s3` only). |
| `OBJECT_STORE_ACCESS_KEY` | `agentic_ops` | Access key (`s3` only). |
| `OBJECT_STORE_SECRET_KEY` | `""` | Secret key (`s3` only). |
| `OBJECT_STORE_SECURE` | `False` | Use TLS against the endpoint (`s3` only). |
| `OBJECT_STORE_GCP_PROJECT` | `""` | GCS project id; optional, the client can infer it from ADC. |

### Runtime bundles

| Env var | Default | Purpose |
| --- | --- | --- |
| `RUNTIME_BUNDLE_ROOT` | `""` | Ephemeral local build/cache path for workflow bundles and synced config snapshots. |
| `RUNTIME_BUNDLE_URI_TEMPLATE` | `""` | Static bundle URI template, e.g. `gs://bucket/bundles/{workflow}.tar.gz`. |
| `RUNTIME_BUNDLE_OBJECT_STORE_BUCKET` | `""` | Canonical bucket for immutable workflow release manifests, config snapshots, and bundles; runtimes receive presigned HTTPS bundle URLs. |
| `RUNTIME_BUNDLE_PRESIGNED_URL_EXPIRES_SEC` | `3600` | Presigned URL validity window. |

### Runtime launcher and memory sync

| Env var | Default | Purpose |
| --- | --- | --- |
| `RUNTIME_LAUNCHER` | `docker` | `docker` or `kubernetes`. |
| `MEMORY_SYNC_MODE` | `docker_volume` | `docker_volume` for Compose or `filesystem` for a mounted memory path. Kubernetes Jobs use task-local `/memory` plus object-store restore/upload. |
| `MEMORY_FILESYSTEM_ROOT` | `/memory` | Container path agent memory volumes mount at. |
| `KUBERNETES_MEMORY_HELPER_IMAGE` | `""` | Image containing `session_manager.memory_sync`; Helm defaults it to the session-manager image. |
| `KUBERNETES_BOOTSTRAP_SECRET` | `""` | Bootstrap Secret name referenced by Kubernetes memory helper Jobs for object-store credentials. Helm sets it from `bootstrap.existingSecret`. |
| `KUBERNETES_NAMESPACE` | `default` | Kubernetes launcher namespace. |

### Gateway / UI

| Env var | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_HOST` | `0.0.0.0` | Gateway bind address. |
| `GATEWAY_PORT` | `8080` | Gateway bind port. |
| `GATEWAY_EVENT_URL` | `http://gateway:8080/events` | Event-collector URL the runtime posts to. |
| `GATEWAY_PUBLIC_BASE_URL` | `""` | Publicly reachable gateway URL (message-bus webhook callbacks). |
| `CONTROL_PLANE_UI_URL` | `""` | Public control-plane UI URL. |

### Message bus

| Env var | Default | Purpose |
| --- | --- | --- |
| `MESSAGE_BUS_PROVIDER` | `mattermost` | `mattermost` or `slack`. Also settable via `platform-config.yaml`'s `message_bus.provider`. |
| `MESSAGE_BUS_API_URL` | `""` | Message bus API base URL. |
| `MESSAGE_BUS_TEAM_NAME` | `""` | Default team for message routing. |
| `MESSAGE_BUS_BOT_TOKEN` | `""` | Bot token (normally supplied encrypted via `platform-config.yaml`'s `secrets:`). |
| `MESSAGE_OUTGOING_WEBHOOK_SECRET` | `""` | Shared secret validating inbound webhook authenticity. |

### Hindsight memory

| Env var | Default | Purpose |
| --- | --- | --- |
| `HINDSIGHT_URL` | `http://hindsight:8888` | Hindsight service base URL. |
| `HINDSIGHT_REQUEST_RETRIES` | `3` | Retry count for Hindsight API calls. |
| `HINDSIGHT_REQUEST_RETRY_BACKOFF_SEC` | `0.5` | Initial retry backoff. |

### Housekeeping / retention

| Env var | Default | Purpose |
| --- | --- | --- |
| `HOUSEKEEPING_ENABLED` | `True` | Enable the periodic background job. |
| `HOUSEKEEPING_INTERVAL_SEC` | `3600` | How often it runs. |
| `BACKGROUND_JOB_RUN_HISTORY_LIMIT` | `5` | How many of its own past runs to keep. |
| `TASK_ARCHIVE_AFTER_DAYS` | `14` | Archive completed tasks after N days. |
| `TASK_DELETE_AFTER_DAYS` | `0` | Delete archived tasks after N days (`0` = never). |
| `LEARNING_MEMORY_RETENTION_DAYS` | `30` | Hindsight learning-bank retention. |
| `AGENT_MEMORY_VERSIONS_TO_KEEP` | `10` | Versioned agent-memory snapshots to retain per workflow. |
| `AGENT_MEMORY_RETENTION_DAYS` | `90` | Delete agent-memory snapshots older than N days. |

### Secrets (age encryption)

| Env var | Default | Purpose |
| --- | --- | --- |
| `AGE_PUBLIC_KEY` | `""` | Recipient public key (`age1...`) used to encrypt new secrets. Safe to commit. |
| `AGE_IDENTITY` | `""` | Private key used to decrypt secrets at container-spawn time. Bootstrap-only, never committed. Accepts an armored key string or `file:/path/to/key.txt`. |

### LLM API key

`LLM_API_KEY` is operator-owned and lives only in the bootstrap
layer — it is the single model-access key passed to a model gateway or direct
LiteLLM-compatible endpoint, Hindsight's LLM and embeddings clients, and
referenced from `model_profiles` entries in `platform-config.yaml` as
`${LLM_API_KEY}`. There is no per-provider key variable; a workflow
selects a model via `session.model` in `agent.yaml`, which resolves to a
`model_profiles` entry (or a raw model name). Hindsight can use that same
endpoint for both clients, but its embedding model must still be an actual
embedding model exposed by that endpoint; it is configured independently from
the chat model.

### Compose environment files

For local Compose deployments, two files are loaded automatically by the
public Makefile:

1. `workflow-repo/deploy/compose.env` is committed and contains **only
  non-secret** values used before services can read `platform-config.yaml`
  (for example `PG_DB`, `PG_USER`, object-store access key, Hindsight base
  URLs/models, and connector instance IDs).
2. `agentic-ops-platform/compose.env` is generated by `make bootstrap`,
  gitignored, and contains the direct-container secrets: `AGE_IDENTITY`,
  `LLM_API_KEY`, `PG_PASSWORD`, and `OBJECT_STORE_SECRET_KEY`.

The generated file is loaded second, so it wins on any accidental collision.
Application settings and ordinary workflow credentials remain in the workflow
repo's `platform-config.yaml`/`agent.yaml`; services that can decrypt them do
so after startup. Postgres, MinIO, and Hindsight cannot, so their direct
secrets belong in the bootstrap layer.

### Loading order

At import time, `shared/lib/config.py` calls `load_platform_env` (see
`shared/lib/platform_secrets.py`) against `PLATFORM_CONFIG_FILE`, decrypts its
`secrets:` block if `AGE_IDENTITY` is set, and overlays each resulting value
onto `Settings` **only if the corresponding env var isn't already set** — so
an explicit bootstrap env var always wins over the repo's config file.

## Layer 2 — `platform-config.yaml` reference

This file lives in the workflow repo (see
[examples/workflow-repo/platform-config.example.yaml](../examples/workflow-repo/platform-config.example.yaml)
for the public template) and is read only after the platform has
cloned/synced the repo.

```yaml
config:            # plain instance env vars (DB/object-store overrides, custom
                    # workflow env, INSTANCE_NAME, AGE_PUBLIC_KEY, ...)

mcps:
  enabled: [...]    # which MCP servers this instance runs
  config:           # per-server policy, keyed by server id (see docs/mcps.md)

connectors:
  enabled: [...]    # which connector instances this deployment runs
  instances:        # instance configs, keyed by instance id (see docs/connectors.md)

message_bus:
  provider: mattermost | slack

runtime_bundles:
  storage: s3
  bucket: agentic-ops-bundles
  retention_versions: 20

runtime_env:        # env injected into every runtime container; a value of
                    # `null` removes a built-in var instead of setting it
  DISABLE_TELEMETRY: true

default_model_profile: local

model_profiles:      # named env-var bundles selected via agent.yaml session.model
  local:
    ANTHROPIC_BASE_URL: http://local-llm:8000
    ANTHROPIC_AUTH_TOKEN: ${LLM_API_KEY}
    ANTHROPIC_MODEL: some-model-name

memory:
  backend: hindsight
  banks:             # workflow name -> Hindsight bank id, per kind
    business:
      my-workflow: incident-rca-my-workflow
    learning:
      my-workflow: workflow-learning-my-workflow

secrets:              # age-encrypted platform-wide secrets
  MESSAGE_BUS_BOT_TOKEN:
    encrypted: "ENC[age,...]"
```

`${VAR}` placeholders anywhere in this file are expanded from `config:`
values, decrypted `secrets:` values, and the process environment at load time
(`shared/lib/platform_secrets.py::expand_env_placeholders`).

### Encrypted secrets

Secrets are age-encrypted (X25519, via the `pyrage` library) and stored as
`ENC[age,<base64-ciphertext>]`. `AGE_PUBLIC_KEY` (safe to commit) encrypts new
values; `AGE_IDENTITY` (bootstrap-only, never committed) decrypts them at
container-spawn time. Use `make set-secret` to encrypt and write a new value.

Per-workflow secrets follow the same `encrypted: ENC[...]` shape under a
workflow's own `agent.yaml` `secrets:` block — see
[Workflow authoring](workflow-authoring.md).

## Layer 3 — workflow packages

`workflows/`, shared `skills/`/`hooks/`, and any custom `mcps/`/`connectors/`
the workflow repo ships. See [Workflow authoring](workflow-authoring.md) for
the package layout and bundle assembly rules.
