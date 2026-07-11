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
layer (`AGE_IDENTITY`, `MODEL_GATEWAY_API_KEY`, workflow-repo pointer, pointed
at your repo's local path). Then from `agentic-ops-platform/`, run:

```sh
COMPOSE_PROFILES=model-gateway,salesforce \
docker compose --env-file compose.env -f deploy/docker-compose.yml up --build
```

Without bootstrap, set the same env vars directly instead:

```sh
HOST_WORKFLOW_REPO_PATH=/path/to/my-workflow-repo \
HOST_PLATFORM_CONFIG_FILE=/path/to/my-workflow-repo/platform-config.yaml \
HOST_LITELLM_CONFIG_FILE=/path/to/my-workflow-repo/litellm.config.yaml \
COMPOSE_PROFILES=model-gateway,salesforce \
docker compose -f deploy/docker-compose.yml up --build
```

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
  -f deploy/k8s-values.yaml
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
