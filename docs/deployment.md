# Deployment

## Bootstrap (`make bootstrap`)

`make bootstrap` runs `scripts/bootstrap.py`, a guided prompt that produces
the **layer 1** bootstrap artifact described in
[Configuration](configuration.md) — it never reads or writes the workflow
repo's `platform-config.yaml`.

Prompts:

1. **Deployment target** — `compose` or `kubernetes`.
2. **Workflow source** — `remote` (git URL + ref + optional PAT) or `local`
   (a filesystem checkout path). `compose`
   only supports `local` today (it bind-mounts a checkout); `kubernetes`
   expects `remote`.
3. **Repo URL/ref/PAT** (remote) or **local checkout path** plus optional
   GitHub URL/PAT (local). The local checkout remains the sync source; the
   GitHub values enable version lookup and reflection PRs for that same repo.
   Kubernetes bootstrap also prompts for the target namespace; use the same
   namespace with `helm upgrade`.
4. **AGE identity** — an armored
   `AGE-SECRET-KEY-...` string or a path to a key file. A path is read into
   the generated secret artifact so it works inside containers.
5. **LLM API key** — one
   key shared by runtime model profiles and Hindsight's LLM/embeddings clients.
6. **Postgres password** and **object-store secret key** —
   direct-container secrets required by Compose infrastructure.

Generated artifact per target (none of these are committed):

| Target | Artifact | Apply |
| --- | --- | --- |
| `compose` | `compose.env` | `make up` |
| `kubernetes` | `dist/bootstrap/k8s-secret.sh` | run the script to create/update a `Secret` |

## Kubernetes deployment

Bootstrap creates only the operator-owned `agentic-ops-bootstrap` Secret. It
does not create the repo-owned `platform-config.yaml` Secret, container images,
object-store buckets, or Helm release.

Use the following command after creating/pushing the image tags referenced by
your workflow repo's `deploy/k8s-values.yaml` and provisioning the configured
object-store bucket for workflow releases and agent memory:

```sh
make k8s-deploy \
   K8S_RELEASE=corp-agentic-ops \
   K8S_NAMESPACE=agentic-ops \
   K8S_VALUES_FILE=/path/to/workflow-repo/deploy/k8s-values.yaml \
   K8S_PLATFORM_CONFIG_FILE=/path/to/workflow-repo/platform-config.yaml \
   K8S_PLATFORM_CONFIG_SECRET=corp-agentic-ops-platform-config
```

`k8s-deploy` applies the generated bootstrap Secret, upserts the
`platform-config.yaml` Secret, then runs `helm upgrade --install --wait
--atomic`. To apply later platform-config, MCP/connector, replica, or image-tag
changes, use the same arguments with `make k8s-update`. It upserts the
platform-config Secret and runs the same Helm upgrade without changing bootstrap
credentials. `K8S_NAMESPACE` must match the namespace embedded by
`make bootstrap` in `dist/bootstrap/k8s-secret.sh`.

For platform code changes, first build and push new **immutable** image tags,
update the corresponding `images.*.tag` values, then run `make k8s-update`.
Reapplying `latest` does not necessarily change a Deployment pod template and
therefore does not provide a reliable rollout.

## Which services actually start

Compose gates every optional MCP server and connector behind a profile (see
`deploy/docker-compose.yml`'s `profiles:` entries); nothing reads
`platform-config.yaml` to decide this automatically for you. `make up` derives
the right profile set with `scripts/compose_profiles.py`, which reads the
instance's `platform-config.yaml`
(`$PLATFORM_CONFIG_FILE`/`$HOST_PLATFORM_CONFIG_FILE`, or the bundled example)
and maps its `mcps.enabled`, `connectors.enabled` (by connector `type`), and
`model_profiles` (by `ANTHROPIC_BASE_URL`) to the matching profile names:

```sh
make up
# Computed COMPOSE_PROFILES=gcp-pubsub,model-gateway,salesforce
```

The workflow repo owns a committed `deploy/compose.env` with non-secret
infrastructure coordinates. `make bootstrap` writes the separate, gitignored
root `compose.env` with bootstrap secrets and its path to that workflow file.
The Makefile loads both automatically (workflow file first, generated secrets
second), so a workflow repo using shipped services needs no Compose override.

Workflow Repo **Sync** is the release boundary for workflow packages and
repo-owned per-task runtime settings. After a successful sync, the selected
revision's `platform-config.yaml` is snapshotted with the workflow bundles; new
tasks use that snapshot for model profiles, context limits, and runtime env.
Running tasks retain their launch environment. This behavior is identical for
local-directory and remote Git workflow sources.

Sync does not reconcile long-running service topology. Changes to `mcps.enabled`
or `connectors.enabled` still require an operator deployment action: run
`make down && make up` for Compose, or update Helm/Kubernetes manifests through the
deployment control plane. The Workflow Repo UI should surface this requirement
when those settings change.

Kubernetes (Helm) does **not** have an equivalent automatic derivation today:

- The Helm chart's `mcps.<id>.enabled` / `connectors.<id>.enabled` booleans in
  `values.yaml` directly gate which templates render — but the chart never
  reads `platform-config.yaml` itself, so these booleans have to be kept in
  sync with it by hand.

For remote private workflow repositories, the Helm chart uses an ephemeral
clone cache in Gateway and session-manager plus object storage as the durable
release transport. A successful UI Sync uploads immutable bundles and the
matching `platform-config.yaml`, then advances the active release pointer.
New tasks resolve both from that one release; no workflow-repo PVC is needed.
Kubernetes runtime Jobs use an `emptyDir` mounted at `/memory`; a helper
restores agent memory from object storage before the runtime starts and uploads
it after the runtime signals completion. PostgreSQL, self-hosted MinIO, and
Hindsight remain the only chart-managed components that need durable PVCs.

## Local development (uncommitted working tree)

In the bootstrap prompt, choose the `local` workflow source so
`WORKFLOW_REPO_PATHS` (or, for compose,
`HOST_WORKFLOW_REPO_PATH`/`HOST_PLATFORM_CONFIG_FILE`) points at a plain
filesystem checkout. **Sync now** rebuilds the bundle from whatever is
currently on disk — no git fetch, no commit, no pinned tag required. Object
storage is optional in this mode; a local `RUNTIME_BUNDLE_ROOT` works fine.

## `make` targets

| Target | Does |
| --- | --- |
| `init` | `uv sync --extra dev`. |
| `bootstrap` | Run `scripts/bootstrap.py` interactively. |
| `set-secret` | Interactively encrypt and store a platform or agent secret. |
| `compose-build` | Build all Compose services. |
| `runtime-build` | Build the `ai-ops-agent-runtime` image. |
| `build` | `runtime-build` + `compose-build`. |
| `up` / `down` | Start/stop the local Compose stack. |
| `k8s-deploy` | Apply the bootstrap/config Secrets and install the Helm release. |
| `k8s-update` | Upsert the config Secret and upgrade the Helm release. |
| `restart` | `down` + `build` + `up`. |
| `restart-<service>` | Rebuild and recreate one service, e.g. `make restart-postgres`. |
| `ensure-test-db` | Create the dedicated Postgres test database if missing. |
| `unit-tests` | `tests/unit` (no infra). |
| `service-tests` | `tests/service` against real Postgres. |
| `runtime-tests` | `tests/runtime` against real Postgres + Docker. |
| `test` | All three suites. |
| `clean-test-containers` | Remove dangling test session containers. |
| `format` / `lint` | ruff fix+format / ruff check+format-check. |

## Deployment artifacts (`deploy/`)

- `deploy/docker-compose.yml` — the public base stack (Postgres, MinIO,
  gateway, session-manager, runtime image, control-plane UI, core MCPs).
  Mounts `HOST_PLATFORM_CONFIG_FILE` (default
  `../examples/workflow-repo/platform-config.example.yaml`),
  `HOST_WORKFLOW_REPO_PATH` (default `../examples/workflow-repo/workflows`),
  and `RUNTIME_BUNDLE_ROOT`. Optional profiles: `model-gateway`, `local-llm`,
  `salesforce`, `splunk`, `cloudwatch`, `jira`, `servicenow`, `gcp-pubsub`.
- `deploy/k8s/agentic-ops` — Kubernetes (Helm) chart; instance values select the
   platform-config secret/configmap, ephemeral workflow clone/release caches,
   object-store release bucket, bootstrap Secret, and which MCPs/connectors
   run. Postgres, S3-compatible storage, and Hindsight are external by default;
   a workflow repo can opt into chart-managed pgvector Postgres, MinIO, and
   Hindsight through `infrastructure.*.enabled` in its values file.

Private/customer deployments should commit non-secret Compose/Kubernetes values
in the workflow repo (`deploy/compose.env`, `deploy/k8s-values.yaml`) and use
the generated bootstrap artifact for secrets, rather than editing the public
base stack or adding a Compose override for shipped services.

## Workflow bundles

A **bundle** is the assembled, runnable package for one workflow: `CLAUDE.md`,
`agent.yaml`, `settings.json`, `.mcp.json`, `agents/`, `skills/`, `hooks/`,
and a `manifest.yaml`, merged from the platform core, the workflow repo's
shared assets, and the workflow itself (see
[Workflow authoring](workflow-authoring.md) for assembly/precedence detail).

Every supported launcher (Docker and Kubernetes) consumes the same contract:

| Env var | Meaning |
| --- | --- |
| `WORKFLOW_BUNDLE_PATH` | A mounted local bundle directory. |
| `WORKFLOW_BUNDLE_URI` | `file://`, `s3://`, `gs://`, or a presigned `https://` URL the runtime fetches and extracts. |
| `WORKFLOW_BUNDLE_CHECKSUM` | `sha256:<hex>` of the bundle's `manifest.yaml`; the runtime refuses to start if this doesn't match. |

Bundles are uniformly object-storage-backed: when
`RUNTIME_BUNDLE_OBJECT_STORE_BUCKET` is set, session-manager uploads a freshly
built bundle and generates a short-lived presigned https URL for
`WORKFLOW_BUNDLE_URI`, so the runtime container needs no cloud SDK — just
plain https plus the standard library `tarfile`.

## Sync and versioning

Sync — whether the initial bootstrap sync or the UI **Sync now** button — runs
one pipeline (`shared/lib/workflow_repo_sync.py::sync_workflow_repo`):

1. Fetch the source at the effective ref (pinned ref if set, else the
   bootstrap `WORKFLOW_REPO_REF`) — or read the current working tree in
   local-path mode.
2. Discover `workflows/*/agent.yaml`.
3. Rebuild every bundle and check `manifest.yaml`'s `platform_version` against
   the running platform's version (see [Compatibility policy](#compatibility-policy)).
4. Upload bundles to object storage, if configured.
5. Persist the result — synced ref/commit, discovered workflows, any bundle
   errors, sync status/error, timestamp — to the `control_plane.workflow_repo_state`
   singleton row.

"Update" always means re-syncing to an explicitly pinned ref — never a silent
pull of `main`. Sync never touches bootstrap secrets (repo URL/PAT,
`AGE_IDENTITY`, `LLM_API_KEY`); changing those means re-running
`make bootstrap`.

### Compatibility policy

Compatible within a major version: if a bundle's `platform_version` major
component is newer than the running platform's, that bundle is blocked
("incompatible"); if older, it's built with a warning; if equal, it's OK.

### Gateway endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/platform/workflow-repo` | Current state: source, pinned/default ref, last sync result, discovered workflows, bundle errors. |
| `POST /api/platform/workflow-repo/sync` | Trigger the sync pipeline now ("Sync now"). |
| `POST /api/platform/workflow-repo/pin` | Set the operator-pinned ref (`{"ref": "..."}`), stored in Postgres and used by the next sync. |
| `GET /api/platform/workflow-repo/versions` | List released versions to pin from — GitHub-hosted repos only (queries the GitHub tags API with the bootstrap PAT); returns `[]` for other hosts. |

The control-plane UI's workflow-repo page only lets an operator change the
pinned version and trigger a sync; the repo URL and PAT are bootstrap-owned
and are never editable from the UI.
