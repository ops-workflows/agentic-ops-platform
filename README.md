# Agentic Ops Platform

Agentic Ops is a workflow runtime and control plane for operational AI
agents: it queues incoming work, launches an isolated Claude Code agent per
task with the tools and memory that workflow needs, brokers human
approvals/clarifications, and gives you a web UI over all of it. Workflow
implementations (prompts, skills, connector instances, integration policy)
live in your own workflow repository; this repo is the platform they run on.

## Capabilities

- **Workflow packages and bundle assembly** — a workflow is `agent.yaml` +
  `settings.json` + `.mcp.json` + skills/hooks/agents, merged with shared
  platform/team assets into a versioned, checksummed bundle.
- **Claude Code runtime harness** — ephemeral per-task containers with
  subagents, human-in-the-loop approvals, a built-in clarification
  (`AskUserQuestion`) flow, and a sandboxed-credential deny-list so secrets
  never reach the model's shell.
- **MCP server framework** — a public catalog of core (message, memory,
  platform self-service) and integration (Salesforce, Jira, Splunk,
  CloudWatch) servers, plus a pattern for adding your own.
- **Source connectors and task queue** — pluggable ingestion adapters (Pub/Sub,
  ServiceNow polling, ...) that turn external events into queued tasks, with
  alert coalescing.
- **Message bus adapters** — Mattermost supports inbound webhooks, approvals,
  posting, and reply waiting; Slack supports posting and reply waiting.
- **Hindsight memory integration** — long-term recall and pattern reflection
  across past incidents, plus per-agent memory volumes backed up to object
  storage.
- **Web control plane** — tasks, session replay, schedules, approvals,
  analytics, and platform catalogs (MCPs, connectors, memory, workflow-repo
  sync/versioning).
- **Docker Compose and Helm** deployment packages, with a provider-neutral
  object-storage layer (`s3` or `gcs`).

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full component
breakdown and a diagram of the request/task flow (connector or webhook →
task queue → session-manager → runtime container → MCP servers/message
bus/approvals → completion → memory sync → control-plane UI).

## Quick start

```sh
cp examples/workflow-repo/platform-config.example.yaml platform-config.yaml
docker compose -f deploy/docker-compose.yml up --build
```

This runs the base stack against
[examples/workflow-repo](examples/workflow-repo/) as a starter workflow repo. To
point at your own workflow repo instead:

```sh
HOST_PLATFORM_CONFIG_FILE=/path/to/workflow-repo/platform-config.yaml \
HOST_WORKFLOW_REPO_PATH=/path/to/workflow-repo \
docker compose -f deploy/docker-compose.yml up --build
```

For a guided setup that also handles secrets, remote workflow repos, and
Kubernetes targets, use `make bootstrap` — see
[docs/deployment.md](docs/deployment.md).

## Documentation

| Doc | Covers |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Components and the request/task flow. |
| [docs/configuration.md](docs/configuration.md) | The three config layers, full `platform-config.yaml` reference, bootstrap env reference. |
| [docs/connectors.md](docs/connectors.md) | Connector model, available connectors, and how to add one. |
| [docs/mcps.md](docs/mcps.md) | Core vs. integration MCP servers and their config. |
| [docs/memory.md](docs/memory.md) | Long-term memory (Hindsight), per-agent project memory, hooks, and weekly reflection. |
| [docs/deployment.md](docs/deployment.md) | `make bootstrap`, Compose/Helm, bundle sync and versioning. |
| [docs/security.md](docs/security.md) | Permissions, sandbox/credential controls, bundle integrity, approvals, encrypted secrets. |
| [docs/workflow-authoring.md](docs/workflow-authoring.md) | How to build a workflow package (agent.yaml, skills, hooks) and its bundle. |

## Public and private boundary

This repository intentionally ships no company-specific workflows, Salesforce
policies, private connectors, customer memory-bank maps, or private
deployment overrides — those belong in a workflow repository. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```sh
python -m pytest -q tests/unit          # unit suite, no infra required
python -m ruff check .                  # lint
python -m ruff format --check .         # format check
cd control-plane-ui && npm ci && npm run build   # control-plane UI
```

`make test` runs the full unit + service (real Postgres) + runtime (real
Docker) suite; see the Makefile (`make help`) for every target.

## Roadmap and contributing

See [docs/roadmap/](docs/roadmap/) for planned work and
[CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute.

## License

Apache License 2.0. See [LICENSE](LICENSE).
