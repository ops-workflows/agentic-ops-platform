# Platform Test Suite

See **[COVERAGE.md](COVERAGE.md)** for a full mapping from every platform
feature to the specific test functions that exercise it.

The suite is split across **three suites with distinct responsibilities**:

- **runtime/** — Layer 2 scenario suite. Boots the real Claude Code SDK +
  CLI inside the runtime container and drives end-to-end scenarios
  through fake LLM / Message / Hindsight / MCP services. This is the
  *primary* feature-coverage suite (40 scenarios).
- **service/** — Layer 1 service integration suite (real Postgres). Now
  scoped to queue semantics, scheduler registration, API error shaping,
  and storage behaviors that are genuinely better tested without the
  full runtime.
- **unit/** — Layer 0 pure helper suite. Fast, no infrastructure, runs
  per-commit. Helpers that cannot be exercised end-to-end at runtime.

Playwright UI tests and real-Hindsight upgrade-compatibility tests are
deliberately out of scope.

## Mock LLM modes

The mock LLM (`tests/fakes/mock_llm.py`) supports two response modes that
runtime scenarios pick between freely:

1. **Scripted mode** — a list of `Turn` objects produces fixed
   tool_use / text / end_turn responses. Use this for behavioral
   scenarios (multi-turn, approvals, AskUserQuestion, MCP tool flows).
2. **Probe / echo mode** — a per-turn `probe` callable inspects the
   inbound LLM request and returns a compact summary string of the
   shape `found_markers=[...] missing_markers=[...]`. Use this for
   instruction-surface tests where you only need to know what the
   runtime sent upstream (e.g. CLAUDE.md markers, skill markers, tool
   exposure).

Helpers live alongside the mock:
- `make_marker_probe(markers)` — returns a probe that summarises
  marker presence
- `scan_markers(body, markers)` — used by tests on recorded requests
- `request_text_blob(body)` — concatenates system+messages+tools

## Layout

```
tests/
  README.md                      — this file
  COVERAGE.md                    — strategy → test mapping
  conftest.py                    — pytest fixtures (root)
  fakes/                         — fake external services
    message.py                — fake Message REST API + wait_for_post
    hindsight.py                 — fake Hindsight service
    mock_llm.py                  — scripted + probe-mode LLM server
    mcp_testserver.py            — FastMCP test server
  fixtures/
    repo-root/
      workflows/platform-test/     — synthetic test plugin
        agent.yaml, settings.json, .mcp.json,
        CLAUDE.md, agents/, skills/, hooks/, README.md
      shared/
        CLAUDE.md                — shared instruction marker
        platform-config.yaml     — base profile (rewritten per session)
        skills/test-shared-skill/SKILL.md
  unit/                          — Layer 0: pure unit tests
  service/                       — Layer 1: service integration tests (real Postgres)
  runtime/                       — Layer 2: runtime scenario tests (real container)
    scenarios/__init__.py        — Scenario DSL + probe_then_end helper
    test_happy_path_scenario.py
    test_approval_scenario.py
    test_mcp_header_scenario.py
    test_timeout_scenario.py
    test_instruction_surface_scenario.py
    test_subagent_and_questions_scenario.py
    test_hooks_scenario.py
    test_memory_scenario.py
    test_mcp_variants_scenario.py
    test_lifecycle_scenario.py
    test_credentials_and_telemetry_scenario.py
```

## Running

The Makefile at the repo root wraps the common commands:

| Make target            | What it runs |
| ---------------------- | ------------ |
| `make unit-tests`      | Layer 0 — fast helpers, no infra |
| `make service-tests`   | Layer 1 — real Postgres |
| `make runtime-tests`   | Layer 2 — full runtime scenario suite |
| `make test`            | All three suites |
| `make ensure-test-db`  | Create the dedicated `agentic_ops_test` DB on port `55432` |
| `make runtime-build`   | Build `ai-ops-agent-runtime:latest` |
| `make clean-test-containers` | Remove dangling test session containers |

### Layer 0 (no infra required)

```
make unit-tests
# or:
uv run pytest tests/unit -q
```

### Layer 1 (Postgres)

Requires a dedicated Postgres test DB. The Make targets handle creation
via `make ensure-test-db`; manually:

> ⚠️ **The suite DROPs the `control_plane` and `task_queue` schemas at the
> start of each session.** It refuses to run unless the database name
> looks like a test DB (suffix `_test` or prefix `test_`) or you set
> `TEST_ALLOW_DB_WIPE=1`. Do **not** point this at your dev `agentic_ops`
> database.

```
make ensure-test-db
make service-tests
```

### Layer 2 (runtime scenario — requires Docker + runtime image)

Requires `TEST_RUNTIME_ENABLED=1`, Postgres via `TEST_DATABASE_URL`, a
running Docker daemon, and `ai-ops-agent-runtime:latest`.

```
# macOS + Rancher Desktop (the docker socket is not at /var/run):
export DOCKER_HOST=unix:///Users/$USER/.rd/docker.sock
make runtime-tests
```

The Layer 2 conftest auto-skips every scenario unless
`TEST_RUNTIME_ENABLED=1` is set.

### Isolated stack scoping

Runtime tests already use:

- in-process FastAPI fakes on free ports for LLM / Message / Hindsight / MCP
  (no shared network with developer services)
- a dedicated test DB (`agentic_ops_test`) with a name-suffix safety guard
- per-test TRUNCATE for table isolation
- per-spawn dynamic platform-config.yaml so model/MCP routing points at
  the test ports
- a local object-store harness for project-memory backup/restore tests

Set `COMPOSE_PROJECT_NAME=aiops-test` (the Make default) to ensure any
compose state created by the run is namespaced apart from the
developer's normal `docker compose` services.

## Markers

- `unit` — Layer 0, always runs
- `service` — Layer 1, needs Postgres
- `scenario` — Layer 2, needs Docker + runtime image

## Counting coverage

Headline number is the **runtime scenario count** (Layer 2). Unit and
service tests are supporting suites and should not be the primary claim
for any feature.

## Notes

- The mock Anthropic server implements the minimum `/v1/messages` surface +
  streaming SSE needed by the runtime scenario tests. It is NOT a full
  Anthropic clone — only what the pinned Claude CLI needs.
- SQLite is never a substitute for queue semantics;
  Layer 1 always uses real Postgres.
- UI (Playwright) tests are not included. Feature coverage is enforced
  at the gateway API layer instead.
