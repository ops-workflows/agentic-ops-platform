# Plugin-local CLAUDE.md (platform-test)

This file is staged from the plugin directory into the runtime workspace.
Tests assert that this marker string reaches the assistant's system prompt:

PLATFORM_TEST_PLUGIN_CLAUDE_MD_MARKER

Plugin-specific guidance:
- This plugin is synthetic and exists only for the regression suite.
- Treat every prompt as a test scenario.
