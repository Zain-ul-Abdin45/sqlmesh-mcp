# Test cases

Plain-English index of every case the test suite covers, and why it exists.
Both files run against `examples/demo_project`, a real SQLMesh project
(generated with `sqlmesh init duckdb`, not hand-written fixtures) backed by
DuckDB.

Run everything: `pytest`. Run just one file: `pytest tests/test_server.py`.

## `tests/test_server.py` — direct function calls

Calls the tool functions directly, bypassing the MCP protocol layer. Fast,
and proves our own logic is correct in isolation.

| Test | What it checks |
|---|---|
| `test_list_models_returns_the_demo_project_models` | All three demo models are returned, with correct kind and columns |
| `test_get_model_includes_rendered_query` | Full model detail includes the actual rendered SQL query |
| `test_get_model_raises_tool_error_with_the_real_message_for_unknown_model` | Looking up a nonexistent model raises `ToolError`, not a generic exception |
| `test_lineage_traces_column_to_upstream_model` | Column-level lineage correctly traces `full_model.num_orders` back to `incremental_model` |
| `test_run_test_passes_on_the_untouched_demo_project` | Unit tests run and pass against the unmodified demo project |
| `test_list_environments_is_empty_before_anything_is_applied` | No environments exist before any plan has been applied |
| `test_plan_previews_the_initial_environment_without_applying` | `plan()` shows all three models as new, normalizes their names to plain `schema.model` form, and does **not** apply anything |
| `test_apply_plan_refuses_without_confirm` | `apply_plan()` without `confirm=true` raises `ToolError`, nothing gets applied |
| `test_apply_plan_rejects_unknown_plan_id` | Applying a `plan_id` that was never previewed raises `ToolError` rather than silently doing nothing |
| `test_apply_plan_actually_applies_when_confirmed` | With `confirm=true`, the plan is actually applied and removed from the in-memory cache |
| `test_run_audit_passes_once_the_project_has_been_applied` | Audits pass against real, versioned data (previously **completely untested** — calling this against an unapplied project raises SQLMesh's own `ConfigError`, which the test now confirms is avoided by applying first) |
| `test_diff_environment_shows_no_diff_immediately_after_apply` | Diffing an environment right after applying it correctly shows no changes |
| `test_list_environments_shows_the_applied_environment` | Once a plan is applied, the environment shows up in `list_environments()` |
| `test_run_reports_nothing_to_do_immediately_after_apply` | Running right after `apply_plan` (which already backfilled everything) correctly reports `NOTHING_TO_DO`, not an error |
| `test_run_refuses_without_confirm` | Same `confirm=true` gate as `apply_plan`, verified independently for `run` |
| `test_profile_model_requires_an_applied_model` | Profiling a model with no physical snapshot yet raises `ToolError` with a clear "apply a plan first" message, not a raw DuckDB `CatalogException` |
| `test_profile_model_never_contains_a_literal_email` | `profile_model` on `incremental_model`'s synthetic `customer_email` column returns a generalized format signature (`aaaaa@aaaaaaa.aaa`) and numeric/null stats, but never the literal fabricated email value or even the constant `@example.com` suffix |
| `test_propose_cleansing_plan_tags_the_synthetic_email_column` | `customer_email` is tagged `PII:EMAIL`; `event_date` (a DATE column) is correctly tagged `none`, not a false-positive phone match; the returned `operation_vocabulary` includes `mask`/`drop_column` |
| `test_apply_cleansing_plan_refuses_without_confirm` | Same `confirm=true` gate as `apply_plan`/`run` |
| `test_apply_cleansing_plan_rejects_unknown_operation_before_touching_data` | A plan referencing an operation outside dataveil's closed vocabulary (e.g. `drop_table`) raises `ToolError` before any data is touched |
| `test_apply_cleansing_plan_masks_the_synthetic_email_column` | A `mask`/`hash` plan step actually rewrites `customer_email` to an md5 digest in the model's physical table, verified by reading it back |
| `test_profile_classify_and_apply_are_all_audit_logged` | A `profile_model` → `propose_cleansing_plan` → `apply_cleansing_plan` sequence appends exactly one `profile`/`classify`/`execute_plan` entry each to the audit log (an isolated `tmp_path` file via `DATAVEIL_AUDIT_LOG_PATH`), and the log never contains the literal email value. Runs before the masking tests below, since it needs `customer_email` still unmasked to see it classified `PII:EMAIL`. |
| `test_register_cleansing_plan_validates_the_plan_immediately` | Registering a plan with an unknown operation raises `ToolError` right away, before any approval step — a reviewer should never approve something that would fail later |
| `test_register_cleansing_plan_returns_an_unapproved_pending_plan` | A freshly registered plan comes back with a `plan_id` and `approved: False` |
| `test_approve_cleansing_plan_unknown_id_raises` | Approving a `plan_id` that was never registered raises `ToolError`, not a silent no-op |
| `test_apply_cleansing_plan_requires_plan_id_when_approval_is_required` | With `DATAVEIL_REQUIRE_PLAN_APPROVAL=true`, passing `plan` directly (the default-mode path) is refused — the server must be told to use `plan_id` instead |
| `test_apply_cleansing_plan_rejects_an_unapproved_plan_id` | A registered-but-not-yet-approved `plan_id` is refused, even with `confirm=true` — approval and `confirm=true` are two independent gates |
| `test_apply_cleansing_plan_rejects_a_plan_id_registered_for_a_different_model` | Passing a `plan_id` registered against `incremental_model` while specifying `model_name="full_model"` is refused, catching a caller mismatch rather than silently applying to the wrong table |
| `test_register_approve_and_apply_cleansing_plan_end_to_end` | The full gated flow: register → approve (by someone) → apply with `plan_id` + `confirm=true` actually masks `customer_email`, and the plan is discarded afterward — replaying the same `plan_id` is refused |

## `tests/test_protocol.py` — real MCP client over stdio

Spawns the server as a real subprocess and talks to it the way an actual MCP
client (Claude Desktop, etc.) would — a real JSON-RPC handshake, not a
Python function call. This is the file that caught the most important bug
found during development.

| Test | What it checks |
|---|---|
| `test_lists_all_fifteen_tools` | The server advertises exactly the fifteen tools it should, over the real protocol |
| `test_mutating_tools_are_flagged_destructive_and_not_read_only` | `apply_plan`, `run`, and `apply_cleansing_plan` all carry `destructive_hint=True`, `read_only_hint=False` in their MCP tool annotations — the signal a client uses to warn a user before calling them |
| `test_approve_cleansing_plan_is_neither_read_only_nor_destructive` | A third annotation shape: `approve_cleansing_plan` mutates server-side approval state (`read_only_hint=False`) but never touches real data (`destructive_hint=False`) |
| `test_read_only_tools_are_flagged_read_only` | Every other tool, including `profile_model`/`propose_cleansing_plan`/`register_cleansing_plan`, is correctly flagged `read_only_hint=True` |
| `test_list_models_call_round_trips_real_data` | A real tool call over stdio returns real model data, not just a well-formed empty response |
| `test_apply_plan_without_confirm_is_a_tool_error_with_the_real_message` | **The regression this file exists to catch.** Without raising `ToolError` specifically, the MCP SDK replaces *any* exception with the generic string `"Error executing tool <name>"` and drops the real message entirely — an agent calling `apply_plan` without `confirm=true` would see no indication of what to fix. This test confirms the actual "confirm=true" guidance reaches the client. |
| `test_get_model_for_unknown_model_is_a_tool_error_with_the_real_message` | Same check for SQLMesh's own error message ("Cannot find model...") — confirms the `_translate_errors` decorator correctly forwards `SQLMeshError` text instead of it being swallowed |
| `test_apply_cleansing_plan_without_confirm_is_a_tool_error_with_the_real_message` | Same regression check as `apply_plan`, for the new dataveil-backed tool — confirms `_translate_errors` also forwards `ExecutionError`'s "confirm=true" message over the wire |
| `test_approve_cleansing_plan_unknown_id_is_a_tool_error_with_the_real_message` | Same regression check for `ApprovalError` — confirms its "no pending plan" message reaches the client, not a generic failure |
| `test_register_cleansing_plan_rejects_unknown_operation_over_the_wire` | Confirms `PlanValidationError`'s "unknown operation" message survives the real protocol round trip too |
| `test_session_survives_a_tool_error_and_keeps_working` | A tool error doesn't crash the server process or corrupt the session — proven by making a real, successful call immediately after two failures |

Note: `test_protocol.py` deliberately never calls `apply_plan`/`apply_cleansing_plan` with `confirm=true` against `prod` — unlike `test_server.py`'s direct-call tests (each call clears `get_context`'s cache per test but still shares one real `db.db`), subprocess-spawned sessions here share that same file across the whole file, and an early protocol test actually applying a plan broke `test_server.py`'s later ordering-sensitive "nothing applied yet" assumptions when both files ran in one `pytest` session. Keep it that way, or give any such test its own isolated project directory.

## Why two files instead of one

Direct-call tests are fast and good at proving logic is right. They are
*not* sufficient on their own — this project's error-handling bug (tool
exceptions being silently replaced with a generic message unless they're a
specific SDK exception type) only exists at the protocol boundary, invisible
to any test that just calls the Python function and catches the exception
itself. Both layers are tested because each one catches a different class of
bug the other cannot see.
