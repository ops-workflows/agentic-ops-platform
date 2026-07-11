# Message Communication Skill

This skill documents the platform's provider-neutral message shapes.
The runtime harness handles approvals and clarifying questions, and the session
manager posts final results. When a workflow needs a visible follow-up handoff,
use the message handoff tool rather than silently creating tasks.

## Default Model

- Use your final result text for normal human-facing output
- Use the built-in `AskUserQuestion` tool when the main session needs clarification from the human operator
- Let the harness approval gate handle restricted tools from `permissions.ask`
- Only use direct message tools in a workflow that explicitly exposes them
- Prefer `handoff_task` when another workflow needs both a visible note and a queued task
- Prefer `post_message` for visible side effects that should not replace the final result text
- Prefer `post_rca_summary` only when the workflow specifically wants the structured RCA layout
- Use `ask_approval` only when the harness approval flow is not the right mechanism

### post_message
Post a general message to the configured channel.

```
Tool: mcp_message → post_message
Args:
  channel_id: string (optional; defaults from X-Message-Channel-Id)
  text: string (markdown formatted)
  thread_id?: string (reply to existing thread)
```

### handoff_task
Post a visible handoff note and enqueue a follow-up workflow task.

```
Tool: mcp_message → handoff_task
Args:
  workflow: string
  prompt: string
  text: string
  channel?: string
  channel_id?: string
  thread_id?: string
  metadata?: object
```

### post_rca_summary
Post a formatted RCA summary with structured fields.

```
Tool: mcp_message → post_rca_summary
Args:
  channel_id: string
  summary: {
    case_id: string
    severity: string
    service: string
    root_cause: string
    timeline: string
    impact: string
    remediation: string[]
  }
```

### ask_approval
Legacy direct-post tool. Prefer `permissions.ask` plus the harness approval gate.

```
Tool: mcp_message → ask_approval
Args:
  channel_id: string
  action_description: string
  risk_level: "low" | "medium" | "high"
  details: string
```

## Message Formatting Guidelines

1. **Use markdown**: Headers, bold, code blocks, lists
2. **Include task context**: Always mention task ID and workflow
3. **Be scannable**: Put the most important info first
4. **Do not duplicate the final result**: direct message tools are for visible side effects, not for re-posting the same answer twice

## Templates

### Investigation Started
```markdown
🔍 **Investigation Started**
- **Task**: `{task_id}`
- **Trigger**: {alert_source} — {alert_summary}
- **Service**: {service_name}
- **Severity**: {severity}

Starting investigation. Will post findings when complete.
```

### Investigation Complete
Return the structured RCA in your final result text.

### Workflow Handoff
```markdown
📨 **Workflow Handoff**
- **From**: {source_workflow}
- **To**: {target_workflow}
- **Reason**: {why_follow_up_is_needed}

{short visible summary}
```

### Escalation Notification
```markdown
⚠️ **Escalation Required**
- **Task**: `{task_id}`
- **Reason**: {escalation_reason}
- **Service**: {service_name}
- **Recommended Action**: {action}

Please review and approve/reject the recommended action.
```

### Daily Digest
```markdown
📊 **Daily Incident Digest — {date}**

| Metric | Value |
|--------|-------|
| Incidents Investigated | {count} |
| P0/P1 Escalations | {escalation_count} |
| Avg Resolution Time | {avg_time} |
| Tokens Used | {tokens} |

**Top Patterns**: {patterns}

**Outstanding Actions**:
{action_items}
```

## Error Communication

When a tool fails or investigation cannot complete:
```markdown
❌ **Investigation Error**
- **Task**: `{task_id}`
- **Error**: {error_description}
- **Attempted**: {what_was_tried}
- **Partial Results**: {any_data_gathered}

Manual investigation may be required.
```
