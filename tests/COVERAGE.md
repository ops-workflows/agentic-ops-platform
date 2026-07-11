# Test Coverage Map

This document maps every platform feature to the concrete test functions
that exercise it. Keep this file up to date when tests are added, removed,
or moved.

- **Suite totals:** 198 passed, 0 skipped with Postgres + runtime prerequisites.
- **Unit (Layer 0):** 124 tests — no infra required.
- **Service (Layer 1):** 34 tests — Postgres required (`TEST_DATABASE_URL`).
- **Runtime (Layer 2):** 40 tests — require
  `TEST_RUNTIME_ENABLED=1`, a running Docker daemon (`DOCKER_HOST` pointing
  at your socket), and a freshly built `ai-ops-agent-runtime:latest`
  image (`docker build -t ai-ops-agent-runtime:latest -f runtime/Dockerfile .`).
  On macOS with Rancher Desktop:
  `export DOCKER_HOST=unix:///Users/$USER/.rd/docker.sock`.

Out of scope (no coverage, by design):

- Playwright UI smoke tests — feature coverage is enforced at the gateway
  API layer instead.
- Real-Hindsight upgrade-compatibility tests — rely on Hindsight's own
  tests plus pinned versions in `pyproject.toml`.

---

## Coverage Baseline by Group

The table below shows which feature groups are covered at each layer.

| Group | Coverage baseline | Unit | Service | Runtime | Primary tests |
|---|---|:---:|:---:|:---:|---|
| Intake and routing | Message webhook intake, schedules, connectors, API task creation, workflow handoff, alert coalescing | ✅ | ✅ | — | `test_message_webhook_helpers.py`, `test_gateway_api.py`, `test_task_queue.py::test_coalesce_merges_into_existing_task`, `test_scheduler_and_pause.py::test_schedule_handler_creates_task_and_updates_last_run` |
| Runtime and workspace | real Claude SDK/CLI invocation, staged writable workspace, shared `CLAUDE.md`, shared skills, shared hook executables, plugin-local files | — | ✅ (provisioner only) | ✅ | `test_provisioner.py`, `test_test_plugin_fixture.py`, `test_plugin_dir.py`, `tests/runtime/test_happy_path_scenario.py`, `tests/runtime/test_instruction_surface_scenario.py` |
| Human-in-the-loop | approval requests, approval rejection, `AskUserQuestion`, late-session question reminder, thread polling | ✅ | ✅ | ✅ | `test_approvals_helpers.py`, `test_budget_reminder.py`, `test_concurrent_approvals.py`, `test_event_collector.py::test_approval_requested_creates_pending_approval`, `tests/runtime/test_approval_scenario.py`, `tests/runtime/test_subagent_and_questions_scenario.py` |
| Queue and lifecycle | Postgres queueing, dequeue concurrency, container spawn, heartbeat, lost-task detection, runtime timeout, manual rerun | — | ✅ | ✅ | `test_task_queue.py` (all 6), `test_event_collector.py::test_session_timeout_marks_timed_out`, `test_container_lifecycle_helpers.py`, `tests/runtime/test_timeout_scenario.py`, `tests/runtime/test_lifecycle_scenario.py` |
| Memory and learning | Hindsight recall/retain/reflect, hook-driven recall/retain, project-memory restore from MinIO, project-memory backup to MinIO | ✅ | ✅ | ✅ | `test_memory_catalog.py`, `test_memory_sync.py`, `test_connectors_and_hindsight_helpers.py` (hindsight helpers), `test_fakes.py::test_fake_hindsight_records_retain_and_scripted_recall`, `test_platform_catalogs.py::test_platform_memories_endpoint_returns_shape`, `tests/runtime/test_memory_scenario.py` |
| MCP integration | Message, Hindsight, Platform, test MCP server, header expansion from env and secrets, tool result propagation | ✅ | ✅ | ✅ | `test_plugin_dir.py::test_validate_plugin_dir_*`, `test_test_plugin_fixture.py::test_plugin_mcp_json_references_testserver`, `test_platform_catalogs.py::test_platform_mcp_endpoint_returns_catalog`, `tests/runtime/test_mcp_header_scenario.py`, `tests/runtime/test_mcp_variants_scenario.py` |
| Scheduling and operational control | schedule registration, in-process firing, enable/disable, agent pause/resume | ✅ | ✅ | ✅ | `test_scheduler_cron.py`, `test_scheduler_dst.py`, `test_scheduler_and_pause.py`, `tests/runtime/test_lifecycle_scenario.py` |
| Credentials and config | encrypted secrets, platform config, runtime env overrides, model profile selection, `${VAR}` expansion | ✅ | — | ✅ | `test_crypto_roundtrip.py`, `test_model_profiles.py`, `test_container_lifecycle_helpers.py::test_apply_runtime_env_overrides_*`, `test_plugin_dir.py::test_read_platform_config_*`, `tests/runtime/test_credentials_and_telemetry_scenario.py` |
| Observability | event collector, session event timeline, token/tool/approval/hook events, state transitions | ✅ | ✅ | ✅ | `test_event_collector_helpers.py`, `test_event_collector.py`, `test_large_outputs.py`, `tests/runtime/test_credentials_and_telemetry_scenario.py` |
| Control plane API and UI | tasks, sessions, approvals, schedules, memory surfaces, MCP catalog, connectors catalog, builder API smoke | — | ✅ | — | `test_control_plane_api.py`, `test_platform_catalogs.py`, `test_gateway_api.py`, `test_scheduler_and_pause.py` |
| Security model | `permissions.ask`, `permissions.deny`, sandbox settings loading, secret isolation from prompts | ✅ | — | ✅ | `test_sandbox_patterns.py`, `test_test_plugin_fixture.py::test_plugin_settings_has_both_ask_and_deny`, `test_plugin_dir.py::test_validate_plugin_dir_is_clean`, `tests/runtime/test_instruction_surface_scenario.py` |

Legend:

- ✅ Covered at this layer.
- — Not applicable at this layer.

---

## Functional Test Catalogue

Each section below lists the concrete tests covering that feature area.
A "—" in the Layer 2 column means no full-runtime scenario is needed;
the behavior is fully verified via unit or service tests with mocks and fakes.

### Provisioning, plugin discovery, and workspace staging

| Case | Coverage |
|---|---|
| provisioner discovers synthetic test plugin | `tests/service/test_provisioner.py::test_provisioner_registers_platform_test_plugin` |
| provisioner idempotent on unchanged config | `tests/service/test_provisioner.py::test_provisioner_idempotent_on_unchanged_config` |
| plugin directory discovery | `tests/unit/test_plugin_dir.py::test_discover_plugin_configs_finds_test_plugin` |
| invalid plugin structure rejected | `tests/unit/test_plugin_dir.py::test_validate_plugin_dir_missing_files`, `test_validate_plugin_dir_bad_mcp_json`, `test_validate_plugin_dir_missing_mcpServers_key` |
| missing `agent.yaml` / empty config | `tests/unit/test_plugin_dir.py::test_read_platform_config_missing_returns_empty`, `test_discover_plugin_configs_missing_dir` |
| schedules from `agent.yaml` synced into DB | `tests/service/test_control_plane_api.py::test_schedules_endpoint_returns_schedule_after_provision` |
| plugin fixture shape (coordinator + subagent + skill + hooks + settings) | `tests/unit/test_test_plugin_fixture.py` (6 tests) |
| runtime stages plugin files into writable workspace, shared `CLAUDE.md`/skills/hooks injected | `tests/runtime/test_happy_path_scenario.py::test_runtime_happy_path` (Layer 2) |

### Task intake and routing

| Case | Coverage |
|---|---|
| Message outgoing-webhook valid token → queued task | `tests/service/test_gateway_api.py::test_message_webhook_creates_task_for_known_channel` |
| invalid Message token rejected | `tests/service/test_gateway_api.py::test_message_webhook_invalid_token_returns_403`; `tests/unit/test_message_webhook_helpers.py::test_verify_webhook_token_*` |
| unknown channel → help shortcut | `tests/service/test_gateway_api.py::test_message_webhook_unknown_channel_returns_help`, `::test_message_webhook_help_shortcut` |
| direct `POST /api/tasks` path | `tests/service/test_task_queue.py::test_create_task_persists_metadata_and_emits_event` |
| scheduled task metadata (`triggered_by=scheduler`) | `tests/service/test_scheduler_and_pause.py::test_schedule_handler_creates_task_and_updates_last_run` |
| MCP workflow handoff | Exercised via gateway API wiring in `test_gateway_api.py`; full end-to-end handoff is Layer 2 (deferred; see scope note). |
| alert coalescing within window | `tests/service/test_task_queue.py::test_coalesce_merges_into_existing_task` |
| trigger-word stripping / routing helpers | `tests/unit/test_message_webhook_helpers.py` (all 11 tests) |

### Queue, concurrency, and session lifecycle

| Case | Coverage |
|---|---|
| dequeue one-per-worker, marks running, sets heartbeat | `tests/service/test_task_queue.py::test_dequeue_task_marks_running_and_sets_heartbeat` |
| `SKIP LOCKED` across connections | `tests/service/test_task_queue.py::test_dequeue_skips_locked_across_connections` |
| max-running limit honored | `tests/service/test_task_queue.py::test_dequeue_respects_max_running` |
| completion + heartbeat renewal | `tests/service/test_task_queue.py::test_complete_task_and_heartbeat` |
| success marks task `succeeded`/`completed` | `tests/service/test_event_collector.py::test_session_complete_marks_task_succeeded` |
| failure marks `failed` | `tests/service/test_event_collector.py::test_session_error_marks_failed` |
| timeout marks `timed_out` | `tests/service/test_event_collector.py::test_session_timeout_marks_timed_out` |
| stale container name cleanup before respawn | `tests/unit/test_container_lifecycle_helpers.py::test_cleanup_stale_container_removes_exited`, `::test_cleanup_stale_container_is_noop_when_not_found`, `::test_cleanup_stale_container_refuses_running` |
| container spawn/exit end-to-end, heartbeat expiry to `lost` | Layer 2 (runtime image) |

### Human-in-the-loop: approvals and clarification

| Case | Coverage |
|---|---|
| approval requested → pending row persisted | `tests/service/test_event_collector.py::test_approval_requested_creates_pending_approval` |
| permission callback creates pending approval | `tests/service/test_concurrent_approvals.py::test_permission_callback_event_creates_pending_approval` |
| non-operator callback kinds ignored | `tests/service/test_concurrent_approvals.py::test_permission_callback_non_operator_kind_is_ignored` |
| approve/reject updates only matching tool | `tests/service/test_concurrent_approvals.py::test_approval_result_updates_matching_tool_only` |
| `_find_open_approval` returns latest pending | `tests/service/test_concurrent_approvals.py::test_find_open_approval_returns_latest_pending` |
| approval metadata merge | `tests/unit/test_approvals_helpers.py` (3 tests) |
| end-to-end approve/reject with real runtime | `tests/runtime/test_approval_scenario.py::test_approval_approve`, `::test_approval_reject` (Layer 2) |

### Claude runtime, tooling, and instruction surfaces

| Case | Coverage |
|---|---|
| outbound Anthropic request sequence (semantic markers) | Layer 2: `tests/runtime/test_happy_path_scenario.py` |
| `permissions.deny` blocks denied tools | `tests/unit/test_sandbox_patterns.py` (all 13 tests) |
| plugin local + shared skill available | `tests/unit/test_test_plugin_fixture.py::test_plugin_local_skill_marker_present` (fixture); real staging in Layer 2 |
| subagent (`Agent` tool) scaffolded in fixture | `tests/unit/test_test_plugin_fixture.py::test_plugin_coordinator_and_subagent_are_present` |
| large result handling | `tests/unit/test_large_outputs.py` (8 tests) |

### MCP and external tool integration

| Case | Coverage |
|---|---|
| `.mcp.json` validation | `tests/unit/test_plugin_dir.py::test_validate_plugin_dir_bad_mcp_json`, `::test_validate_plugin_dir_missing_mcpServers_key` |
| MCP catalog endpoint | `tests/service/test_platform_catalogs.py::test_platform_mcp_endpoint_returns_catalog` |
| test MCP server receives headers (round-trip) | `tests/runtime/test_mcp_header_scenario.py` (Layer 2) |
| FastMCP test server harness smoke | `tests/unit/test_test_plugin_fixture.py::test_plugin_mcp_json_references_testserver` |

### Hindsight and hook-driven learning

| Case | Coverage |
|---|---|
| FakeHindsight retain + scripted recall | `tests/unit/test_fakes.py::test_fake_hindsight_records_retain_and_scripted_recall` |
| `/platform/memories` returns shape (including `hindsight_available=False` when unreachable) | `tests/service/test_platform_catalogs.py::test_platform_memories_endpoint_returns_shape` |
| hindsight response parsing helpers | `tests/unit/test_connectors_and_hindsight_helpers.py::test_extract_hindsight_entries_*` (5 tests) |
| bank constants + workflow bank routing | `tests/unit/test_memory_catalog.py` (3 tests) |

### Project memory restore and backup

| Case | Coverage |
|---|---|
| volume name convention (`agent-memory-{name}`) | `tests/unit/test_memory_sync.py::test_volume_name_convention` |
| Docker archive stream concatenation | `tests/unit/test_memory_sync.py::test_stream_to_bytes_concatenates_chunks` |
| tar normalization strips top-level `memory/` dir | `tests/unit/test_memory_sync.py::test_normalize_archive_strips_top_level_dir`, `::test_normalize_archive_preserves_file_contents`, `::test_normalize_archive_empty_input_returns_empty_archive` |
| Memory restore/backup round-trip | `tests/runtime/test_memory_scenario.py::test_memory_restore_from_minio`, `::test_memory_backup_to_minio_on_end` |

### Scheduling, pause/resume, and operational controls

| Case | Coverage |
|---|---|
| cron parsing / next-run computation | `tests/unit/test_scheduler_cron.py` (3 tests) |
| UTC/DST correctness, invalid cron rejected | `tests/unit/test_scheduler_dst.py` (5 tests) |
| schedule firing creates task + updates `last_run` | `tests/service/test_scheduler_and_pause.py::test_schedule_handler_creates_task_and_updates_last_run` |
| APScheduler job execution path queues task after registration | `tests/service/test_scheduler_and_pause.py::test_scheduler_executes_registered_job_and_queues_task` |
| agent pause/resume toggles flag | `tests/service/test_scheduler_and_pause.py::test_agent_pause_and_resume_toggles_paused_flag` |
| pause on unknown agent returns 404 | `tests/service/test_scheduler_and_pause.py::test_pause_unknown_agent_returns_404` |
| schedule enable/disable endpoint | `tests/service/test_control_plane_api.py::test_schedules_endpoint_returns_schedule_after_provision` |

### Credentials, config, and secret handling

| Case | Coverage |
|---|---|
| age encrypt/decrypt round-trip, bad key fails | `tests/unit/test_crypto_roundtrip.py` (3 tests) |
| model profile selection + `ANTHROPIC_BASE_URL` wiring | `tests/unit/test_model_profiles.py` (6 tests) |
| `${VAR}` + scalar runtime-env overrides, null-removes | `tests/unit/test_container_lifecycle_helpers.py::test_apply_runtime_env_overrides_*` (2 tests) |
| agent env collection from `agent.yaml` | `tests/unit/test_container_lifecycle_helpers.py::test_agent_env_vars_*` (3 tests) |
| platform-config loading | `tests/unit/test_plugin_dir.py::test_read_platform_config_returns_parsed_agent_yaml`, `::test_read_platform_config_missing_returns_empty` |
| session model selector fallbacks | `tests/unit/test_container_lifecycle_helpers.py::test_session_model_selector_*` (2 tests) |

### Observability, telemetry, and session replay

| Case | Coverage |
|---|---|
| conversation batch updates tokens | `tests/service/test_event_collector.py::test_conversation_batch_updates_tokens` |
| approval/completion/error/timeout transitions | `tests/service/test_event_collector.py` (6 tests total) |
| empty task id is safe | `tests/service/test_event_collector.py::test_event_with_empty_task_id_is_safe` |
| token-count coercion helpers | `tests/unit/test_event_collector_helpers.py` (7 tests) |
| large-output/large-result event payload shape | `tests/unit/test_large_outputs.py` (8 tests) |

### Control plane API and UI

| Case | Coverage |
|---|---|
| tasks list | `tests/service/test_control_plane_api.py::test_tasks_endpoint_lists_seeded_task` |
| approvals list (empty counts) | `tests/service/test_control_plane_api.py::test_platform_approvals_endpoint_reports_empty_counts` |
| schedules endpoint | `tests/service/test_control_plane_api.py::test_schedules_endpoint_returns_schedule_after_provision` |
| memories endpoint | `tests/service/test_platform_catalogs.py::test_platform_memories_endpoint_returns_shape` |
| connectors endpoint | `tests/service/test_platform_catalogs.py::test_platform_connectors_endpoint_lists_shipped_connectors` |
| MCP catalog endpoint | `tests/service/test_platform_catalogs.py::test_platform_mcp_endpoint_returns_catalog` |
| unknown route → 404 | `tests/service/test_gateway_api.py::test_unknown_route_returns_404` |
| health | `tests/service/test_gateway_api.py::test_health_endpoint` |
| UI smoke | **Out of scope** — Playwright intentionally dropped; UI coverage relies on API-layer tests above. |

### Builder and platform proposal surfaces

| Case | Coverage |
|---|---|
| builder handler inline (no pure helpers) | Not independently unit-tested — the builder endpoint's only non-trivial logic is validation that lives inside a frontier-LLM-invoking handler. Future work. |

### Connectors

| Case | Coverage |
|---|---|
| connector catalog discovery (public connectors) | `tests/unit/test_connectors_and_hindsight_helpers.py::test_read_connectors_catalog_finds_public_connectors`, `::test_connector_shape_contains_target_and_tags` |
| source-label derivation | `tests/unit/test_connectors_and_hindsight_helpers.py::test_connector_source_label_*` (3 tests) |
| identifier humanizer | `tests/unit/test_connectors_and_hindsight_helpers.py::test_humanize_identifier_strips_separators` |
| connectors endpoint | `tests/service/test_platform_catalogs.py::test_platform_connectors_endpoint_lists_shipped_connectors` |

### Additional behaviors

#### Hook telemetry and full hook surface

| Case | Coverage |
|---|---|
| plugin hooks point at executable python files | `tests/unit/test_test_plugin_fixture.py::test_plugin_hooks_point_at_executable_python_files` |
| end-to-end hook execution + telemetry | Layer 2 (runtime image required) |

#### Claude SDK workarounds

| Case | Coverage |
|---|---|
| permission callback/bridge semantics | `tests/service/test_concurrent_approvals.py` (5 tests) |
| runtime pins + stages Claude CLI | Layer 2 |

#### Sandboxing and security boundary

| Case | Coverage |
|---|---|
| Bash glob / exact prefix / other-tool patterns | `tests/unit/test_sandbox_patterns.py::test_bash_*` (4 tests) |
| MCP wildcard/prefix/single-tool/other-server patterns | `tests/unit/test_sandbox_patterns.py::test_mcp_*` (4 tests) |
| Read/Write patterns | `tests/unit/test_sandbox_patterns.py::test_read_*`, `::test_write_wildcard_matches_any_file` (3 tests) |
| exact name match + negative | `tests/unit/test_sandbox_patterns.py::test_exact_tool_name_match`, `::test_exact_tool_name_no_match` |
| settings have both ask + deny | `tests/unit/test_test_plugin_fixture.py::test_plugin_settings_has_both_ask_and_deny` |

#### Turn/time budgets, late-session reminder

| Case | Coverage |
|---|---|
| fires on turn budget (14/20) | `tests/unit/test_budget_reminder.py::test_reminder_fires_on_turn_budget` |
| fires on time budget (450/600 s) | `tests/unit/test_budget_reminder.py::test_reminder_fires_on_time_budget` |
| suppressed when disabled / already-sent / result-text / min-turns / recent-question | `tests/unit/test_budget_reminder.py::test_reminder_suppressed_*` (5 tests) |
| reminder text non-empty | `tests/unit/test_budget_reminder.py::test_reminder_text_is_nonempty` |
| subagent-no-output retry text | `tests/unit/test_budget_reminder.py::test_subagent_no_output_retry_text` |
| question-response parser (single, free-text, multi, out-of-range) | `tests/unit/test_budget_reminder.py::test_parse_question_response_*` (4 tests) |

#### Thread polling and mid-session user messages

| Case | Coverage |
|---|---|
| webhook trigger-word / comment-shape helpers | `tests/unit/test_message_webhook_helpers.py::test_comment_response_shape`, `::test_strip_trigger_word_*` |
| end-to-end thread polling | Layer 2 (runtime image required) |

#### Concurrent approvals and `AskUserQuestion` in one session

| Case | Coverage |
|---|---|
| concurrent approvals for different tools | `tests/service/test_concurrent_approvals.py::test_concurrent_approvals_for_different_tools` |
| approval result updates only matching tool | `tests/service/test_concurrent_approvals.py::test_approval_result_updates_matching_tool_only` |
| `_find_open_approval` latest-pending selection | `tests/service/test_concurrent_approvals.py::test_find_open_approval_returns_latest_pending` |
| ask-user-question callback is not persisted as an approval | `tests/service/test_concurrent_approvals.py::test_permission_callback_non_operator_kind_is_ignored` |

#### Workflow handoff and alert coalescing

| Case | Coverage |
|---|---|
| coalesce merges into existing task | `tests/service/test_task_queue.py::test_coalesce_merges_into_existing_task` |
| workflow handoff end-to-end | Layer 2 / deferred |

#### Large outputs and file handling

| Case | Coverage |
|---|---|
| inline threshold (10 KB) | `tests/unit/test_large_outputs.py::test_max_inline_size_threshold_is_10kb` |
| preview truncation (2000 chars) | `tests/unit/test_large_outputs.py::test_extract_tool_result_preview_truncates_to_2000` |
| error flag preserved | `tests/unit/test_large_outputs.py::test_extract_tool_result_error_flag` |
| empty / missing blocks safe | `tests/unit/test_large_outputs.py::test_extract_tool_result_returns_none_*`, `::test_extract_tool_result_none_when_no_tool_result_block` |
| terminal event payload with large parts + empty-parts omitted | `tests/unit/test_large_outputs.py::test_build_terminal_event_payload_*` (2 tests) |
| subagent-no-output detection | `tests/unit/test_large_outputs.py::test_is_subagent_no_output_result_detects_marker` |

#### Gateway APIs, auth, and input validation

| Case | Coverage |
|---|---|
| endpoint shapes | `tests/service/test_gateway_api.py`, `tests/service/test_control_plane_api.py`, `tests/service/test_platform_catalogs.py` |
| auth token verification | `tests/service/test_gateway_api.py::test_message_webhook_invalid_token_returns_403`; `tests/unit/test_message_webhook_helpers.py::test_verify_webhook_token_*` |
| unknown-route 404 | `tests/service/test_gateway_api.py::test_unknown_route_returns_404` |

#### Scheduler correctness details

| Case | Coverage |
|---|---|
| cron field-count validation | `tests/unit/test_scheduler_dst.py::test_parse_cron_rejects_wrong_field_count` |
| `compute_next_run` future ISO / invalid returns None | `tests/unit/test_scheduler_dst.py::test_compute_next_run_returns_future_isoformat`, `::test_compute_next_run_returns_none_on_invalid` |
| UTC evaluation | `tests/unit/test_scheduler_dst.py::test_cron_trigger_uses_utc_not_local_time` |
| DST spanning 72 hours — no duplicates, strictly increasing | `tests/unit/test_scheduler_dst.py::test_cron_trigger_spans_dst_boundary_without_duplicates` |

#### Container cleanup and resource hygiene

| Case | Coverage |
|---|---|
| stale container removed when exited | `tests/unit/test_container_lifecycle_helpers.py::test_cleanup_stale_container_removes_exited` |
| NotFound is a no-op | `tests/unit/test_container_lifecycle_helpers.py::test_cleanup_stale_container_is_noop_when_not_found` |
| running container raises (refuses silent kill) | `tests/unit/test_container_lifecycle_helpers.py::test_cleanup_stale_container_refuses_running` |
| full lifecycle (spawn → exit → cleanup → volume preserved) | Layer 2 |

---

## Gaps / deferred

Items still not covered by automated tests (deliberately or pending):

- Builder API smoke — handler validation is inlined with an LLM call; no separable helpers to unit test today. Tracked as future work.
- Workflow handoff end-to-end (MCP `create_task` from within a session) — only the coalesce path is covered at the service layer; the handoff path requires a runtime scenario.
- Real MinIO round-trip for memory backup/restore — the helpers are unit tested; the MinIO leg is stubbed at the service layer. A dedicated integration test is deferred.

---

## Layer 2 runtime scenarios

The runtime suite is the **primary feature-coverage layer**. Unit and
service tests provide supporting coverage for individual helpers and
infrastructure behaviors.

### Instruction surface

| Scenario | Coverage |
|---|---|
| Shared CLAUDE.md present in system prompt | `tests/runtime/test_instruction_surface_scenario.py::test_shared_claude_md_present_in_system_prompt` |
| Plugin CLAUDE.md present | `tests/runtime/test_instruction_surface_scenario.py::test_plugin_claude_md_present_in_system_prompt` |
| Plugin-local skill reachable | `tests/runtime/test_instruction_surface_scenario.py::test_plugin_local_skill_marker_reachable` |
| Shared skill reachable | `tests/runtime/test_instruction_surface_scenario.py::test_shared_skill_marker_reachable` |
| permissions.deny blocks tool | `tests/runtime/test_instruction_surface_scenario.py::test_permissions_deny_blocks_tool` |
| Sandbox loaded into runtime | `tests/runtime/test_instruction_surface_scenario.py::test_sandbox_loaded_in_runtime` |
| Skill tool invocation recorded for UI sidebar | `tests/runtime/test_instruction_surface_scenario.py::test_skill_invocation_recorded_in_conversation_batch` |

### Subagents and AskUserQuestion

| Scenario | Coverage |
|---|---|
| Subagent delegation via Agent/Task tool | `tests/runtime/test_subagent_and_questions_scenario.py::test_subagent_delegation` |
| Subagent no-output retry path | `tests/runtime/test_subagent_and_questions_scenario.py::test_subagent_no_output_retry` |
| AskUserQuestion approve | `tests/runtime/test_subagent_and_questions_scenario.py::test_ask_user_question_approve` |
| AskUserQuestion multi-select | `tests/runtime/test_subagent_and_questions_scenario.py::test_ask_user_question_multi_select` |
| Approval timeout / no reply | `tests/runtime/test_subagent_and_questions_scenario.py::test_approval_timeout_no_reply` |
| Late-session AUQ reminder | `tests/runtime/test_subagent_and_questions_scenario.py::test_late_session_question_reminder` |

### Hooks

| Scenario | Coverage |
|---|---|
| UserPromptSubmit hook fires once | `tests/runtime/test_hooks_scenario.py::test_user_prompt_submit_hook_fires_once` |
| PreToolUse hook fires per tool_use | `tests/runtime/test_hooks_scenario.py::test_pretool_hook_fires_per_tool_use` |
| SubagentStop hook fires | `tests/runtime/test_hooks_scenario.py::test_subagent_stop_hook_fires` |
| Failing hook does not kill session | `tests/runtime/test_hooks_scenario.py::test_failing_hook_does_not_kill_session` |

### Memory and Hindsight

| Scenario | Coverage |
|---|---|
| Auto-recall on session start | `tests/runtime/test_memory_scenario.py::test_hindsight_auto_recall_on_session_start` |
| Explicit retain via tool | `tests/runtime/test_memory_scenario.py::test_hindsight_retain_via_tool` |
| Memory restore from MinIO | `tests/runtime/test_memory_scenario.py::test_memory_restore_from_minio` |
| Memory backup to MinIO | `tests/runtime/test_memory_scenario.py::test_memory_backup_to_minio_on_end` |

### MCP variants

| Scenario | Coverage |
|---|---|
| Message MCP routes through fake | `tests/runtime/test_mcp_variants_scenario.py::test_message_mcp_post_routes_through_fake` |
| Platform MCP create_task | `tests/runtime/test_mcp_variants_scenario.py::test_platform_mcp_create_task_creates_task_row` |
| MCP server error → is_error=true | `tests/runtime/test_mcp_variants_scenario.py::test_mcp_tool_returns_error` |
| MCP very large result offload | `tests/runtime/test_mcp_variants_scenario.py::test_mcp_large_result_offloaded` |

### Lifecycle, queue, scheduler, and connectors

| Scenario | Coverage |
|---|---|
| Heartbeat updates across turns | `tests/runtime/test_lifecycle_scenario.py::test_heartbeat_updates_across_turns` |
| Lost-task detection | `tests/runtime/test_lifecycle_scenario.py::test_lost_task_detection` |
| Manual rerun resets failed task | `tests/runtime/test_lifecycle_scenario.py::test_manual_rerun_resets_failed_task` |
| Scheduler firing spawns runtime | `tests/runtime/test_lifecycle_scenario.py::test_scheduler_firing_spawns_runtime` |
| Paused agent does not spawn | `tests/runtime/test_lifecycle_scenario.py::test_paused_agent_does_not_spawn` |
| Connector intake without message_thread | `tests/runtime/test_lifecycle_scenario.py::test_connector_task_without_message_thread_posts_top_level` |

### Credentials, telemetry, large output, and final post

| Scenario | Coverage |
|---|---|
| Secret expansion + isolation from LLM body | `tests/runtime/test_credentials_and_telemetry_scenario.py::test_secret_var_expanded_in_mcp_header_and_isolated_from_llm` |
| Secret env var denied to sandboxed Bash | `tests/runtime/test_credentials_and_telemetry_scenario.py::test_secret_var_denied_in_sandboxed_bash` |
| Credential deny-list generation (host-independent) | `tests/unit/test_sandbox_credential_denies.py` |
| Runtime env null removes variable | `tests/runtime/test_credentials_and_telemetry_scenario.py::test_runtime_env_null_override_removes_variable` |
| Final result posted in correct thread | `tests/runtime/test_credentials_and_telemetry_scenario.py::test_final_result_posted_in_correct_thread` |
| Session detail API returns timeline | `tests/runtime/test_credentials_and_telemetry_scenario.py::test_session_detail_api_returns_event_timeline` |

The default runtime run executes the full Layer 2 suite when
`TEST_RUNTIME_ENABLED=1` is set.
