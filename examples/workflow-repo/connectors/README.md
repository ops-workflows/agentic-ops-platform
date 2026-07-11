# Connectors

Source connectors read from external systems and write tasks to the platform
task queue. Generic connector implementations (GCP Pub/Sub, ServiceNow) live
in the public platform repo under `connectors/`; a workflow repo only
configures **instances** of them through `platform-config.yaml`, and can add
its own custom connector implementations here.

See [docs/connectors.md](../../../docs/connectors.md) in the platform repo
for the connector model and the full instance schema for each shipped
connector.

## Enabling and configuring an instance

```yaml
connectors:
  enabled:
    - my-connector-instance      # instance ids, not directory names
  instances:
    my-connector-instance:
      type: gcp-pubsub           # a public connector implementation, or one
                                  # defined in connectors/<name>/ here
      source: { ... }
      target: { ... }
      parsing: { ... }
      coalescing: { ... }
```

`connectors.enabled` lists instance ids to run and surfaces them in the
platform connectors catalog. `connectors.instances.<id>` holds the full
instance definition; `${VAR}` placeholders resolve from `config:` values,
`secrets:`, and the process environment at load time. The running container
selects its instance via the `CONNECTOR_INSTANCE_ID` env var in your
deployment override.

## Adding a custom connector

See [custom-connector-example/](custom-connector-example/) for a minimal
skeleton to copy and adapt:

1. Add `connectors/<name>/main.py` here (or in the platform repo, if it's
   generic enough to be reusable), loading its instance with
   `shared.lib.platform_secrets.load_connector_instance(path, instance_id)`
   and enqueuing tasks via `shared.lib.task_queue.create_task()`.
2. Define one or more instances of it under `connectors.instances` in
   `platform-config.yaml`.
3. Wire the connector service in your deployment override, setting
   `CONNECTOR_INSTANCE_ID` to the instance id.
