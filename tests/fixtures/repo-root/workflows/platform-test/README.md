# Platform test plugin

This is the synthetic test plugin used by the platform regression suite.

It is intentionally small and not tied to any business workflow.

The plugin exercises:

- one default coordinator agent
- one helper subagent
- one plugin-local skill
- one local `UserPromptSubmit` hook and one `SubagentStop` hook
- one `.mcp.json` pointing at the FastMCP test server
- one `settings.json` with both `permissions.ask`, `permissions.deny`, and sandbox settings
- one cron schedule

No part of this plugin reaches out to real external services.
