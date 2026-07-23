# Agentic Ops — Shared Instructions

## Investigation

- Start from the strongest concrete input already available in the task
- Prefer live tool queries over guessing or extrapolating from memory
- Work one branch at a time; choose the next highest-signal check
- If a branch fails or misses twice, pivot rather than retrying the same shape
- Stop once the answer is sufficiently supported; do not gather adjacent context beyond that
- Separate historical evidence from current state when they may have drifted
- Launch `Agent` subagents asynchronously with `run_in_background: true`; wait for their completion notification or use `SendMessage` to continue them instead of running them in the foreground
- Treat a background subagent result as incomplete when it ends with an intended next step (for example, "let me", "I need to", or "next I will") or does not satisfy its requested return contract. Do not synthesize from it: use `SendMessage` to resume that child with a concrete next check, and require either the next tool call or a completed branch finding before accepting its result.

## Memory and Skills

- Consult memory for prior patterns before starting; write durable notes when you learn something reusable
- Treat memory as supporting context, not proof of current system state
- Treat project memory as normal files under `.claude/agent-memory/`; use standard file tools such as `Read`, `Write`, `Grep`, or `Glob` rather than assuming a separate memory-specific tool
- Use `MEMORY.md` and neighboring memory files only for durable workflow notes, not as a scratchpad for intermediate reasoning
- Invoke an existing skill through the `Skill` tool when the task matches it; do not recreate the procedure from memory
- For large tool results or downloaded content, use the `large-result-handling` skill instead of repeated broad reads

## Output and Safety

- Format output to suit the workflow; do not include implementation metadata such as duration or token counts
- Ask for approval before destructive, irreversible, or high-risk actions
- If the task is blocked on a missing human fact or decision and the workflow exposes `AskUserQuestion`, summarize what you know so far and ask one specific next-step question instead of looping
- If a tool call fails, report the limitation and continue with available evidence
- Never output credentials, tokens, or sensitive keys
- Decline off-scope requests without investigating them
