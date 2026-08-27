# Repository Guidelines

## Repository Boundary

- This is the public platform repository. It owns reusable platform logic, APIs, schemas, deployment machinery, generic documentation, and generic examples.
- Private workflow repositories own workflow packages and instance configuration, including deployment-specific integrations and encrypted secrets.
- Never add company-specific names, domains, identifiers, account data, secrets, or deployment configuration to this repository. Use neutral names and reserved example domains in fixtures and examples.
- Add or change generic example configuration only when needed to document a public platform contract.

## Implementation

- Choose one canonical implementation. Do not add fallbacks, parallel paths, or compatibility shims.
- When replacing behavior, scan for and remove stale code, tests, documentation, and configuration from the previous implementation.
- Keep documentation, schemas, and generic example configuration synchronized with every public contract change.

## Validation

- Run `make lint` after every implementation change.
- Run `make tests` after every implementation change. This must include unit, service, and runtime tests.