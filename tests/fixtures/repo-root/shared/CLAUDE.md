# Shared CLAUDE.md (test fixture)

This file is staged into every runtime session by the platform's
workspace builder. Tests assert that this exact marker string reaches
the assistant's system prompt:

PLATFORM_TEST_SHARED_CLAUDE_MD_MARKER

Operational rules for the platform-test agent:

- Do exactly what the prompt asks. No more, no less.
- Do not improvise tool calls.
- When in doubt, end the turn cleanly.
