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

## Runtime isolation layers

Claude Code, the runtime container, and the host each enforce a different
boundary. They are complementary; no one layer replaces the others.

### Claude Code and Bubblewrap

[Claude Code's sandbox](https://code.claude.com/docs/en/sandboxing) is an
operating-system-enforced boundary for the `Bash` tool and every process Bash
starts. On Linux, Claude Code uses
[Bubblewrap](https://github.com/containers/bubblewrap) to construct a namespace
with an explicit filesystem view, restricted writable paths, and controlled
network access. Its permission rules still govern all tools before they run,
including MCP, Read, Edit, and WebFetch.

This is deliberately **not** a sandbox for the Claude Code harness or for the
runtime container as a whole. Built-in file tools use Claude's permission
system, and the parent Claude process retains its normal container privileges.
The credential deny-list below therefore prevents a sandboxed Bash process from
reading a secret; it does not make that secret unavailable to the trusted
runtime process that needs it for MCP authentication.

Bubblewrap is a low-level sandbox constructor, not a complete security policy.
It uses Linux facilities such as mount and user namespaces to create the inner
environment, so its availability depends on the outer runtime permitting those
operations. In particular, an unprivileged container must be able to create the
namespaces Bubblewrap needs. Treat a failure to initialize the Claude sandbox as
a deployment configuration failure in production; configure Claude Code to fail
closed rather than silently running Bash outside the sandbox.

### gVisor for the runtime container

[gVisor](https://gvisor.dev/) is the recommended outer isolation boundary for
untrusted agent-session containers. Docker selects it through its `runsc` OCI
runtime. Instead of letting the container directly exercise the host kernel as
an ordinary OCI container does, gVisor interposes a user-space application
kernel between the workload and the host. This substantially reduces the host
kernel attack surface exposed to a compromised container, while retaining the
container's normal filesystem, process, and network model.

gVisor is not a VM and does not replace deployment policy. Operators must still
minimize mounts, avoid Docker sockets and host paths, use non-root runtime
users, constrain egress, apply resource limits, and keep images and gVisor
patched. The Compose deployment selects `runsc` only for ephemeral agent
session containers when `SANDBOX_MODE=gvisor`; the long-running platform
services continue to use their configured Docker runtime.

Together, the layers look like this:

```text
host kernel
  -> gVisor/runsc isolates the agent-session container
    -> Claude Code/Bubblewrap isolates Bash and its child processes
      -> permission and credential rules constrain individual tool use
```

The outer gVisor layer protects the host from a container compromise. The inner
Bubblewrap layer narrows what model-directed shell commands can access inside an
otherwise valid session container. Both layers need compatibility testing for
the selected gVisor release and Claude Code version.

### Providing an outer sandbox

**Linux VM with Docker Compose.** Install a supported `runsc` binary using the
[gVisor installation guide](https://gvisor.dev/docs/user_guide/install/), then
register it with Docker and restart Docker:

```sh
sudo runsc install
sudo systemctl restart docker
docker run --runtime=runsc --rm hello-world
```

After `make bootstrap`, start the platform with:

```sh
SANDBOX_MODE=gvisor make up
```

The session manager then requests `runtime="runsc"` for each agent-session
container. Keep `runsc` as an explicitly selected runtime rather than changing
Docker's global default unless every workload on the VM has been qualified for
it.

**Kubernetes.** A plain Pod is not an agent-sandbox product by itself. On GKE,
[GKE Agent Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/machine-learning/agent-sandbox)
is a managed implementation designed for isolated, stateful agent workloads;
it uses the open-source Agent Sandbox controller and supports hardened runtimes
such as gVisor. For other Kubernetes distributions, evaluate and operate the
[Kubernetes Agent Sandbox](https://kubernetes.io/blog/2026/03/20/running-agents-on-kubernetes-with-agent-sandbox/)
CRDs plus a compatible runtime such as gVisor or Kata Containers. Confirm that
the cluster's pod security, seccomp, AppArmor, and user-namespace policies also
allow the desired inner Bubblewrap behavior before treating the configuration as
production-ready.

**Cloud Run.**
[Cloud Run sandboxes](https://cloud.google.com/blog/topics/developers-practitioners/google-cloud-run-sandboxes-are-in-public-preview)
are in public preview and provide a platform-managed execution boundary with
credential/environment isolation, deny-by-default egress, and a temporary
filesystem overlay. They are promising for untrusted code execution, but this
platform has not yet qualified Claude Code's Bubblewrap sandbox inside them.

**Local development.**
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) provide a separate
microVM-based local environment with its own Docker daemon, filesystem, and
network. They can be explored on supported macOS or Windows developer machines;
organization governance features require the relevant Docker subscription. This
platform has not yet qualified Claude Code/Bubblewrap inside Docker Sandboxes,
so do not rely on that combination for production isolation without a dedicated
compatibility test.

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
Compose and Helm artifacts are templates — production deployments
must provide instance-owned TLS, IAM, network policy, database and
object-storage credentials, and secret management.
