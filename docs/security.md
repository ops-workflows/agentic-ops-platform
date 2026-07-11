# Security Model

This is the authoritative security reference for the platform. For how to
report a vulnerability, see [SECURITY.md](../SECURITY.md).

The platform keeps workflow prompts, runtime control, and credentials in
separate layers: what a workflow is *allowed* to do (permissions), what a
sandboxed shell can *see* (credential deny-list), what a bundle is *trusted*
to contain (integrity), and how secrets are *stored* (age encryption).

## Permissions model

Each workflow ships a native Claude Code `settings.json` with `permissions`
and `sandbox` rules; the runtime enforces them for headless execution:

- `permissions.deny` becomes the SDK's `disallowed_tools` — those tools can
  never run.
- `permissions.ask` is enforced through the SDK's `can_use_tool` callback and
  routes each matching call through the [approval flow](#approval-gating)
  instead of executing it automatically. Example: exposing a mutating
  Salesforce tool would add `mcp__salesforce__<tool>` to `permissions.ask`.
- The main workflow session gets Claude Code's built-in tools unless they are
  explicitly restricted; subagents only get the tools listed in their
  markdown frontmatter. An agent expected to ask clarifying questions must
  include `AskUserQuestion` in its allowlist.

The ephemeral per-task Docker container is one isolation layer, but it is not
the only one — the Claude-native permission and sandbox rules are the first
policy layer for agent behavior.

## Sandbox credential deny-list

Runtime containers must give the model shell access to Bash while never
letting the model read secret values through it. `runtime/session_entrypoint.py`
handles this at workspace-staging time:

1. Every declared secret name is collected from the workflow's `agent.yaml`
   `secrets:` block and the instance's `platform-config.yaml` `secrets:`
   block (names only — nothing is decrypted for this step).
2. Those names are merged into the bundle's `.claude/settings.json` under
   `sandbox.credentials.envVars` with `mode: "deny"`, alongside any entries
   the workflow already authored there.
3. Claude Code's sandbox executor unsets each listed variable before every
   sandboxed Bash invocation, so `echo $SECRET_TOKEN` returns empty inside
   Bash.
4. The parent Claude process keeps the real values in its own environment, so
   `${VAR}` expansion in `.mcp.json` headers (MCP auth) still works normally.

This only takes effect when the sandbox itself is active (namespaces/bubblewrap
available). Where it isn't (e.g. some local dev hosts), containment falls back
to Docker's own process/filesystem isolation plus the workflow's permission
rules — this is a known, expected local-only gap, not a production behavior.

## Bundle integrity

- **Checksum verification.** session-manager computes
  `sha256:<hex(manifest.yaml)>` when it resolves/builds a bundle and passes it
  as `WORKFLOW_BUNDLE_CHECKSUM`. The runtime recomputes the same hash after
  extracting the bundle and refuses to start the session on a mismatch.
- **Path-traversal protection.** Before extracting a bundle tarball, every
  tar member's resolved destination path is checked to still be inside the
  destination directory; any member that would escape it (`../..`) aborts
  extraction instead of being written.
- **Plaintext-secret scanning at build time.** `shared/lib/workflow_bundles.py`
  scans bundle YAML/JSON for keys that look like secrets (`secret`, `token`,
  `password`, `api_key`, `private_key`, case-insensitive) and rejects a bundle
  build if a matching key holds a plaintext-looking value instead of a
  `${VAR}` placeholder, `ENC[...]`, or an obvious placeholder string.

## Approval gating

Tool calls that match a workflow's permission ask-list (glob patterns such as
`Bash(systemctl *)`, `mcp__splunk__*`, configured in the bundle's
`settings.json`) are not executed automatically:

1. The runtime creates an `Approval` row (`status: "pending"`) and posts a
   prompt with approve/reject buttons to the message bus
   (`gateway/approval_broker.py`).
2. A human's decision arrives via a message-bus action webhook, which updates
   the `Approval` row and records a `SessionEvent`.
3. The runtime, which is polling approval status, proceeds or fails the tool
   call based on that decision — the model never has an unmediated path to
   run a gated action.

## Encrypted secrets (age)

Secrets are asymmetrically encrypted with [age](https://age-encryption.org)
(X25519) via the `pyrage` library, stored as `ENC[age,<base64 ciphertext>]`:

- `AGE_PUBLIC_KEY` (safe to commit) encrypts new values — used by `make
  set-secret` and any gateway-side secret-entry flow.
- `AGE_IDENTITY` (bootstrap-only, **never committed**) decrypts values at
  container-spawn time, both for platform-wide secrets in
  `platform-config.yaml` and per-workflow secrets in `agent.yaml`. It accepts
  either an armored `AGE-SECRET-KEY-...` string or `file:/path/to/key.txt`.
- Decrypted values only ever exist in-memory in the process that needs them
  (gateway for encryption, session-manager for decryption at launch, the
  runtime container's environment) — never written back to disk in plaintext.

## Secret handling rules

- Never commit plaintext secrets. Secret values do not belong in prompts,
  `CLAUDE.md`, skills, or `.mcp.json` — `.mcp.json` stores `${VAR}`
  placeholders, not resolved values.
- Keep workflow-specific secrets in the workflow repo (`agent.yaml`, or a
  deployment secret manager); keep platform-wide secrets encrypted in
  `platform-config.yaml`'s `secrets:`, or in Secret Manager / Kubernetes
  Secrets.
- Do not add encrypted secrets from a private deployment to the public repo.
- MCP servers are stateless request handlers, not secret stores: auth travels
  as per-request `${VAR}` headers so per-workflow credentials never enter the
  MCP server trust boundary.
- Do not expose secret env vars to Bash unless a workflow explicitly needs
  shell access and its sandbox policy allows it.

## Supported scope and public/private boundary

The public security boundary covers the mechanisms this repo owns:

- runtime bundle staging, checksum verification, and path-traversal safety
- the sandbox credential deny-list
- MCP auth-header expansion without exposing secret values to the model shell
- gateway approval and human-question flows
- platform config and encrypted-secret loading
- the deployment templates under `deploy/`

A workflow repo is responsible for its own prompts, skills, hooks, connector
instances, private MCP policy, and which tools/domains it permits. The public
Compose, Helm, and Cloud Run artifacts are templates — production deployments
must provide instance-owned TLS, IAM, network policy, database and
object-storage credentials, and secret management.
