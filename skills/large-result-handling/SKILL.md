---
name: large-result-handling
description: Narrow oversized tool outputs and saved result files without wasting turns or tokens
---

# Large Result Handling

Use this skill when a tool response is too large, a result is saved to a file, or the useful evidence is buried in a long blob.

## Core Rule

- Do not loop on broad `Read` calls over the same large file or payload.
- Narrow first, then inspect only the exact fragment that matters.

## Preferred Pattern

1. Identify the exact target you need.
   Examples: class name, helper method, record ID, error code, line number, validation message, JSON field.
2. Use `Grep` or targeted `Bash` extraction to locate the anchor.
3. If the payload is structured JSON, use `jq` to extract the exact field or record into a temp file.
4. Use `sed -n` or a narrow `Read` only on the small surrounding range.
5. Once the root-cause hypothesis is already supported by the narrowed evidence, stop reading and summarize.

## Patterns

### Structured JSON payloads

- Prefer `jq` over manual scanning.
- Example: `jq -r '.records[0].Body' <tool-result-file> > /tmp/extracted.txt`

### Minified or long-line files

- Do not rely on `Read` line offsets alone.
- Extract the exact field or object first, then inspect the extracted text.

### Source or metadata bodies

- Use `Grep` to find the exact symbol, then inspect only the surrounding lines.
- Example: `sed -n '520,560p' /tmp/apex-body.cls`

### Inline MCP payloads (not saved to a file)

- When a large body (for example an Apex `Body` field) is returned inline in an
  MCP tool result rather than as a file path, first persist it to a file with the
  `Write` tool — do not try to embed the text in a Bash heredoc or `python3`
  string, which mangles quotes and newlines.
- Example: `Write` the body to `/tmp/apex-body.cls`, then `sed -n '260,280p' /tmp/apex-body.cls`
  or `grep -n 'execute' /tmp/apex-body.cls` to reach the failing line.

## Anti-Patterns

- Do not re-read the same full file repeatedly.
- Do not sample broad unrelated sections after the likely failing path is already visible.
- Do not treat token truncation as a reason to keep widening the read window.

## Reporting

- In the final answer, mention the exact narrowed artifact you inspected, such as the extracted JSON field, the helper method, or the specific line range.