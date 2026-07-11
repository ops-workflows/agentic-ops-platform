---
name: "{MAIN_AGENT}"
description: "{What this agent does}"
memory: project
tools:
  - Read
  - Skill
  - TodoWrite
  - WebFetch
  - Write
  - Edit
  - Bash
  - Grep
  - Agent
mcpServers:
  - message
  - memory
  - platform
---

You are a **{ROLE}** in an Agentic Ops workflow.

## Your Mission
{Describe what the agent should do when activated}

## Procedure
1. {Step 1}
2. {Step 2}
3. {Step 3}

## Output Format
{Describe expected output format}