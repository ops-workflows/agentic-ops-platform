---
name: reflect
description: Run the scheduled weekly workflow reflection over retained learning traces and propose skill improvements via PR when warranted
---

# Weekly Workflow Reflection

Use this skill for the scheduled weekly workflow-reflection run. The input is not a single RCA. The input is the last week of retained workflow-learning traces for this workflow.

> **Requires**: The agent must have `platform` in its `mcpServers` list to propose updates.

## When to Reflect

- During the scheduled weekly workflow-reflection run
- When reviewing the last 7 days of retained workflow-learning traces for a workflow

## Reflection Procedure

### Step 1: Synthesize Weekly Learning Patterns
- Call `reflect_patterns` with `bank_kind="learning"` and `time_range="7d"`
- Ask about the last 7 days and about workflow behavior, not business incident themes
- Focus on recurring investigation patterns such as:
   - repeated tool loops
   - wrong tool choice
   - wrong or weak parameters
   - unneeded tool calls before the useful one
   - signals that consistently led to resolution
   - skills or instructions that were missing, stale, or misleading
- If `reflect_patterns` fails or times out, note that explicitly and stop. Do not fall back to `recall_similar` just because reflection failed.

### Step 2: Compare Patterns With Current Guidance
- Read only the current workflow-repo guidance before proposing changes:
   workflow-local skills/instructions and shared skills under the workflow
   repository's `skills/` directory. Read `manifest.yaml` first: only assets
   whose `assets.sources` value is `team` or `workflow` are eligible. Assets
   marked `core` come from the public platform and must not be reviewed,
   optimized, or proposed for modification.
- Check whether the weekly patterns are already covered
- Look for gaps between what long-term memory says the workflow did across the week and what the current guidance tells the agent to do

### Step 3: Identify Improvements

Look for:
1. **Repeated waste**: Did the workflow repeatedly make unnecessary or low-signal tool calls?
2. **Wrong steering**: Did the workflow repeatedly choose the wrong tool or weak parameters before finding the right path?
3. **Missing guidance**: Is a useful query, diagnostic check, or decision rule absent from the skills?
4. **Incorrect guidance**: Does a current skill or agent instruction appear misleading based on the week's patterns?
5. **Escalation/documentation gaps**: Should escalation or documentation guidance change?

### Step 4: Propose the Update

If you identified an improvement worth persisting:

1. **Read the current file** using the Read tool to get the exact current content
2. **Compose the full updated content** — make the targeted change while preserving everything else
3. **Submit via `mcp__platform__propose_skill_update`**:
   - `file_path`: repo-relative path (e.g. `workflows/incident-investigator/skills/splunk-queries/SKILL.md`)
   - `content`: the complete updated file
   - `title`: concise description of what changed
   - `description`: evidence from this incident + reasoning

This creates a GitHub PR tagged `[reflect]` for human review. The update only takes effect after a human merges it.

**Updatable files**:
- Workflow skills: `workflows/{workflow}/skills/{skill}/SKILL.md`
- Workflow agents: `workflows/{workflow}/agents/{agent}.md`
- Shared skills: `skills/{skill}/SKILL.md`

All paths are relative to the bootstrapped workflow repository. The PR always
targets that repository; platform-core files are never PR targets.

If the issue is documentation drift rather than investigation procedure drift, create a visible message handoff to the `documentation` workflow instead of proposing a skill update.

### Step 5: Return the Weekly Reflection Result

- Summarize the weekly patterns you found
- State whether a PR was proposed
- If no update was warranted, say that explicitly and explain why the current guidance was sufficient

## Quality Criteria for Skill Updates

- Is the new pattern supported by the weekly learning traces rather than a one-off run?
- Does it make future investigations faster or more accurate?
- Is it general enough to be useful across incidents (not one-off)?
- Does it follow the existing skill format conventions?

## Anti-Patterns

- Don't treat this as a single-session retrospective
- Don't mix business incident digesting with workflow-improvement reflection
- Don't add overly specific patterns that only apply to one customer/case
- Don't remove existing patterns without strong evidence they're wrong
- Do keep skill files focused — one concept per section
