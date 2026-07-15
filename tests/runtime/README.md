# Layer 2 — Runtime scenario tests

These tests spawn real Docker containers running the session entrypoint
against fake services (mock LLM, fake Message, fake Hindsight, FastMCP
test server) and assert on the end-to-end session outcome.

## Prerequisites

```
docker build -t ai-ops-agent-runtime:latest -f runtime/Dockerfile .
make ensure-test-db
export TEST_DATABASE_URL=postgresql+asyncpg://agentic_ops:localdev-postgres-password@localhost:55432/agentic_ops_test
export TEST_RUNTIME_ENABLED=1
```

## Running

```
make runtime-tests
# or
.venv/bin/python -m pytest tests/runtime --timeout=180 --timeout-method=thread -q
```

## How ANTHROPIC_BASE_URL is wired

The test fixtures use the platform's existing `model_profiles` mechanism.
A test-specific `platform-config.yaml` is generated dynamically by the
runtime conftest with a `test` model profile whose `ANTHROPIC_BASE_URL`
points at the mock LLM server's port via `host.docker.internal`.

The flow:

1. `tests/runtime/conftest.py` starts the mock LLM on a free port.
2. It writes a `platform-config.yaml` with `model_profiles.test.ANTHROPIC_BASE_URL`
   set to `http://host.docker.internal:{port}`.
3. `shared.lib.config.settings.platform_config_file` is patched to this file.
4. The test fixture `agent.yaml` sets `session.model: test`.
5. `spawn_agent_session()` calls `load_platform_runtime_env()` which
   resolves the `test` profile and merges `ANTHROPIC_BASE_URL` into
   the container's environment.
6. Inside the container, the Claude Code CLI reads `ANTHROPIC_BASE_URL`
   and sends LLM requests to the mock server.

## Implemented scenarios

The runtime suite is the **primary feature-coverage layer**.
It is broken into focused scenario files:

| File | Area | Scenarios |
|------|---------------|-----------|
| `test_happy_path_scenario.py` | baseline | Single text turn; tool_use + end_turn |
| `test_approval_scenario.py` | baseline | Approval approve / reject |
| `test_mcp_header_scenario.py` | baseline | MCP header env-var expansion |
| `test_timeout_scenario.py` | baseline | Runtime timeout enforcement |
| `test_instruction_surface_scenario.py` | instruction surface | Shared/plugin CLAUDE.md, plugin/shared skill, deny enforcement, sandbox |
| `test_subagent_and_questions_scenario.py` | subagents and operator interaction | Subagent delegation, no-output retry, AskUserQuestion approve / multi-select, approval timeout, late-session reminder |
| `test_hooks_scenario.py` | hooks | UserPromptSubmit, PreToolUse counter, SubagentStop, failing hook tolerance |
| `test_memory_scenario.py` | memory and hindsight | Auto-recall, retain via tool, restore, backup |
| `test_mcp_variants_scenario.py` | MCP variants | Message MCP, Platform MCP create_task, MCP error, MCP large result |
| `test_lifecycle_scenario.py` | lifecycle | Heartbeat, lost-task, manual rerun, scheduler firing, paused agent, connector intake |
| `test_credentials_and_telemetry_scenario.py` | credentials and telemetry | Secret expansion + isolation, env null override, final-post threading, session detail API |

Total: 40 runtime scenarios across 11 files.

## Mock LLM modes

- **Scripted mode** — pass `Turn(respond=[...], stop_reason=...)` blocks
  to drive multi-turn behavior (tool use, approvals, MCP).
- **Probe / echo mode** — pass `Turn(probe=callable)` to inspect the
  upstream LLM request and return a compact summary like
  `found_markers=[...] missing_markers=[...]`. Use
  `make_marker_probe(markers)` from `tests.fakes.mock_llm` to build the
  callable.

The default runtime run executes the full scenario suite. No separate
opt-in collection gate is required beyond `TEST_RUNTIME_ENABLED=1`.
