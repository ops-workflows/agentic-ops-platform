# Shared Skills

This directory is the workflow repo's own shared-skills layer — the
repo-wide equivalent of the platform's own `skills/` at
[agentic-ops-platform/skills/](../../../skills/). Skills placed here are
injected into every workflow's bundle alongside the platform's shared skills,
the same way shared `hooks/` are.

## Precedence

Skill assembly follows the same shadow rule as the rest of a bundle (see
[Bundle assembly](../../docs/workflow-authoring.md#bundle-assembly)):

1. **Platform core** — `agentic-ops-platform/skills/*`.
2. **This repo's shared skills** — `skills/*` here.
3. **Workflow-local skills** — `workflows/<name>/skills/*`.

A same-named skill in a later layer replaces the earlier one entirely;
different-named skills from every layer are all available. Don't duplicate a
platform skill here unless you actually need to override its content — an
unmodified copy just drifts out of sync as the platform skill evolves.

## Adding a shared skill

Create `skills/<skill-name>/SKILL.md` here — see
[custom-skill-example/](custom-skill-example/SKILL.md) for the minimal shape
to copy and adapt. Reference it from an agent's `Skill` tool the same way you
would a workflow-local or platform skill; no other wiring is required.
