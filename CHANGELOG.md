# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - Unreleased

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
