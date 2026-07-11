---
name: test-skill
description: Plugin-local skill used only to assert that plugin-local skills are staged and take precedence over same-named shared skills.
---

# Test skill

This skill exists so tests can assert the runtime stages plugin-local skills
into the workspace and that they are visible to the Claude runtime.

Marker string for assertions: `PLATFORM_TEST_PLUGIN_LOCAL_SKILL_MARKER`.
