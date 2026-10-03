# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-03

### Added

- `profile_model`, `propose_cleansing_plan`, `apply_cleansing_plan` -- local,
  aggregate-only data profiling, PII classification, and plan-based
  cleansing (mask/drop/impute/...), powered by
  [`dataveil`](https://github.com/Zain-ul-Abdin45/dataveil). No raw row ever
  reaches this server's profiling path, and a cleansing plan can only
  reference a small, pre-tested operation vocabulary -- never SQL or code an
  LLM wrote itself. `apply_cleansing_plan` requires `confirm=true`. Every
  call to these three tools is appended to a local JSON-lines audit log
  (`.dataveil/audit.jsonl` next to the project by default, override with
  `DATAVEIL_AUDIT_LOG_PATH`) -- who/what/when, never a literal cell value.
- `register_cleansing_plan`, `approve_cleansing_plan` -- an optional second
  gate in front of `apply_cleansing_plan`, independent of `confirm=true`.
  Off by default; set `DATAVEIL_REQUIRE_PLAN_APPROVAL=true` to require a
  plan be registered and approved (by a separate call, potentially a
  separate reviewer) before `apply_cleansing_plan` will run it by
  `plan_id`. Useful once this touches anything with real compliance stakes.

### Changed

- Depends on `dataveil[sqlmesh]>=0.2.0` from PyPI. Format signatures and
  numeric stats follow dataveil 0.2.0's fixes; see its release notes.

## [0.1.0] - 2026-09-17

Initial release.

### Added

- `list_models`, `get_model` -- model metadata and rendered queries.
- `plan`, `apply_plan` -- preview and (with `confirm=true`) apply a plan.
- `lineage` -- column-level lineage for a model's column.
- `run_audit`, `run_test` -- run a model's audits / unit tests.
- `diff_environment`, `list_environments` -- inspect environment state.
- `run` -- execute due scheduled runs for an environment (requires `confirm=true`).
- Protocol-level test suite (`tests/test_protocol.py`) spawning the server
  as a real MCP client would, alongside direct function-call tests.
