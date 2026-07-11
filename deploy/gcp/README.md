# Cloud Run Deployment Templates

These templates are intentionally thin deployment artifacts for the public platform.
They keep the same runtime bundle contract used by Docker and Kubernetes:

- `WORKFLOW_BUNDLE_URI`
- `WORKFLOW_BUNDLE_CHECKSUM`
- `WORKFLOW_BUNDLE_PATH` when a local filesystem bundle is mounted

Use `envsubst`, Terraform, Pulumi, or another deployment system to substitute the
`${...}` placeholders. Full GCP infrastructure such as Cloud SQL, Secret Manager,
GCS buckets, Artifact Registry, IAM, VPC connectors, and schedulers should live in
instance-owned IaC.

Suggested render flow:

```sh
export PROJECT_ID=my-project
export REGION=europe-west1
export REPOSITORY=agentic-ops
export IMAGE_TAG=latest
export BUNDLE_BUCKET=agentic-ops-bundles

envsubst < deploy/gcp/gateway.service.yaml | gcloud run services replace - --region "$REGION"
envsubst < deploy/gcp/session-manager.service.yaml | gcloud run services replace - --region "$REGION"
envsubst < deploy/gcp/control-plane-ui.service.yaml | gcloud run services replace - --region "$REGION"
envsubst < deploy/gcp/runtime-job.yaml | gcloud run jobs replace - --region "$REGION"
```

Render the core MCP services from `mcp.service.yaml` for each always-on module:

```sh
MCP_NAME=message MCP_MODULE=mcps.core.mcp_message MCP_PORT=8100 envsubst < deploy/gcp/mcp.service.yaml | gcloud run services replace - --region "$REGION"
MCP_NAME=memory MCP_MODULE=mcps.core.mcp_memory MCP_PORT=8103 envsubst < deploy/gcp/mcp.service.yaml | gcloud run services replace - --region "$REGION"
MCP_NAME=platform MCP_MODULE=mcps.core.mcp_platform MCP_PORT=8105 envsubst < deploy/gcp/mcp.service.yaml | gcloud run services replace - --region "$REGION"
```

The same generic template renders any optional integration server (only
render the ones your instance actually enables):

```sh
MCP_NAME=salesforce MCP_MODULE=mcps.integrations.mcp_salesforce MCP_PORT=8102 envsubst < deploy/gcp/mcp.service.yaml | gcloud run services replace - --region "$REGION"
```

A **custom** MCP server needs no new template either — render `mcp.service.yaml`
with `MCP_NAME`/`MCP_MODULE`/`MCP_PORT` pointed at your own module, as long as
your `${REPOSITORY}/mcps` image (built from your workflow repo) includes it
alongside the public `mcps/` package, the same requirement as the Kubernetes
chart's shared-image model (see `examples/workflow-repo/deploy/README.md`).

Render each enabled connector from `connector.service.yaml` (only the ones
your instance actually enables, one per `connectors.instances.<id>`):

```sh
CONNECTOR_NAME=servicenow-connector CONNECTOR_INSTANCE_ID=my-servicenow-instance envsubst < deploy/gcp/connector.service.yaml | gcloud run services replace - --region "$REGION"
CONNECTOR_NAME=gcp-pubsub-connector CONNECTOR_INSTANCE_ID=my-connector-instance envsubst < deploy/gcp/connector.service.yaml | gcloud run services replace - --region "$REGION"
```

Connectors are long-running pollers/subscribers, not HTTP request handlers, so
`connector.service.yaml` sets `minScale`/`maxScale` to `1` (always one instance,
no scale-to-zero) instead of relying on request-driven autoscaling. Cloud Run
still requires the container to bind `$PORT` and pass health checks, so both
bundled connectors start a tiny `/health` listener via
`shared.lib.health_server.start_health_server()` — a **custom** connector
needs to do the same (and, like MCP servers, needs its own module baked into
the shared `${REPOSITORY}/connectors` image referenced by this template).

## What is `serving.knative.dev/v1`?

`mcp.service.yaml` (and the other `*.service.yaml` files) are
[Knative Serving](https://knative.dev/docs/serving/) `Service` resources —
Cloud Run's fully-managed product implements the Knative Serving API, so
`gcloud run services replace` accepts this exact YAML shape directly. That's
also why these templates would port with minimal changes to a self-hosted
Knative/Cloud Run for Anthos cluster if that's ever needed.
