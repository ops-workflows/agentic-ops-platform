# Deployment (Example)

This directory shows how to run the public platform's base stack against a
workflow repo checked out as a sibling directory of `agentic-ops-platform`.
This is one of two supported deployment modes — see
[docs/deployment.md](../../../docs/deployment.md) for the other (`make
bootstrap` targeting a remote/pinned workflow repo).

## Docker Compose

The base `deploy/docker-compose.yml` already covers everything a workflow repo
needs — pointing at your repo, and every MCP server/connector the platform
ships — via env vars and `COMPOSE_PROFILES`. **No compose override is needed**
unless you're adding a *custom* MCP server or connector (see
[docker-compose.override.yml](docker-compose.override.yml) for that case).

Run `make bootstrap` in `agentic-ops-platform/` first (see its
`docs/deployment.md`) to generate `compose.env` with the operator bootstrap
layer (`AGE_IDENTITY`, `LLM_API_KEY`, workflow-repo pointer, pointed
at your repo's local path). Copy [compose.env.example](compose.env.example) to
your workflow repo as `deploy/compose.env` and set its non-secret values. Then
from `agentic-ops-platform/`, run:

```sh
make up-auto
```

The Makefile loads `deploy/compose.env` first, then the generated root
`compose.env`, and derives the required profiles from `platform-config.yaml`.

### Adding your own custom MCP server or connector

If your workflow repo defines its own MCP server or connector (see
[mcps/custom-mcp-example/](../mcps/custom-mcp-example/) and
[connectors/custom-connector-example/](../connectors/custom-connector-example/)),
those don't exist in the base compose file and do need an override:

```sh
COMPOSE_PROFILES=custom-example \
docker compose \
  -f ../agentic-ops-platform/deploy/docker-compose.yml \
  -f deploy/docker-compose.override.yml \
  up --build
```

Set these when the repos are not sibling directories:

```sh
export AGENTIC_OPS_PLATFORM_ROOT=/path/to/agentic-ops-platform
export WORKFLOW_REPO_ROOT=/path/to/my-workflow-repo
```

## Kubernetes (Helm)

Use the public chart with your own values file:

```sh
helm upgrade --install my-workflow-repo \
  ../agentic-ops-platform/deploy/k8s/agentic-ops \
  -f deploy/k8s-values.yaml \
  --namespace my-workflow-namespace --create-namespace
```

A values file is needed even without any custom service — it's how you set the
per-instance namespace-level specifics (`platformConfig.existingSecret`,
`workflowRepo.existingClaim`, image repositories, `runtimeBundles.uriTemplate`)
and which shipped `mcps:`/`connectors:` entries are `enabled: true`, mirroring
your `platform-config.yaml`'s `mcps.enabled`/`connectors.enabled` (the chart
doesn't read `platform-config.yaml` itself, so keep the two in sync).

Unlike the compose override, the chart has **no per-service build context** —
every `mcps:`/`connectors:` entry (including `custom-example` here) runs from
one shared image per component (`images.mcp.repository` /
`images.connector.repository`). To add your own MCP server or connector,
build a combined image containing both the public code and your own modules,
publish it as that image, and reference your module's import path the same
way `custom-example` does.

`k8s-values.yaml` has two infrastructure modes. Keep all
`infrastructure.*.enabled` values `false` and set `platformEnv` to use
operator-provided Postgres, S3-compatible storage, and Hindsight. Or set all
three to `true` to provision pgvector Postgres, MinIO, and Hindsight in this
namespace; their credentials still come only from the bootstrap Secret. The
public example shows both modes. Run `make bootstrap`, select `kubernetes`, and
enter the same namespace used by Helm so its generated Secret lands in the
right scope.
