# sqlmesh-mcp

[![CI](https://github.com/Zain-ul-Abdin45/sqlmesh-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Zain-ul-Abdin45/sqlmesh-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sqlmesh-mcp.svg)](https://pypi.org/project/sqlmesh-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/sqlmesh-mcp.svg)](https://pypi.org/project/sqlmesh-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MCP server exposing a [SQLMesh](https://github.com/SQLMesh/sqlmesh) project to LLM agents: model metadata, plan previews, column-level lineage, audits/tests, and environment diffs.

Not officially affiliated with SQLMesh or Tobiko Data.

## Why

SQLMesh's standout feature is column-level lineage, which is exactly the kind of question an agent is good at answering interactively ("where does `revenue` in `finance.daily_summary` come from?") that a CLI isn't. As of writing, the only prior MCP server for SQLMesh ([`sherman94062/sqlmesh-mcp`](https://github.com/sherman94062/sqlmesh-mcp)) is a small unmaintained side project — this one aims to be documented, tested, and kept current with SQLMesh's API.

## Install

```bash
pip install sqlmesh-mcp
```

## Usage

Point it at a SQLMesh project directory:

```json
{
  "mcpServers": {
    "sqlmesh": {
      "command": "sqlmesh-mcp",
      "env": { "SQLMESH_PROJECT_PATH": "/path/to/your/sqlmesh/project" }
    }
  }
}
```

## Example

Calling `list_models` against [`examples/demo_project`](examples/demo_project) (a stock `sqlmesh init duckdb` project) returns:

```json
[
  {
    "name": "sqlmesh_example.full_model",
    "kind": "FULL",
    "description": null,
    "owner": null,
    "tags": [],
    "columns": { "item_id": "INT", "num_orders": "BIGINT" }
  },
  {
    "name": "sqlmesh_example.incremental_model",
    "kind": "INCREMENTAL_BY_TIME_RANGE",
    "description": null,
    "owner": null,
    "tags": [],
    "columns": { "id": "INT", "item_id": "INT", "event_date": "DATE" }
  },
  {
    "name": "sqlmesh_example.seed_model",
    "kind": "SEED",
    "description": null,
    "owner": null,
    "tags": [],
    "columns": { "id": "INT", "item_id": "INT", "event_date": "DATE" }
  }
]
```

From there, `lineage("sqlmesh_example.full_model", "num_orders")` traces that column back to `incremental_model.id` — the kind of question this server exists for.

## Tools

| Tool | Read-only? | Description |
|---|---|---|
| `list_models` | Yes | List all models in the project with kind, columns, description |
| `get_model` | Yes | Full detail for one model |
| `plan` | Yes | Preview what a plan against an environment would change |
| `apply_plan` | **No** | Apply a previously-previewed plan. Requires `confirm=true`. |
| `lineage` | Yes | Column-level lineage for a model's column |
| `run_audit` | Yes | Run a model's audits |
| `run_test` | Yes | Run a model's unit tests |
| `diff_environment` | Yes | Diff two environments |
| `list_environments` | Yes | List every environment that exists in the project's state |
| `run` | **No** | Execute scheduled/due model runs for an environment (what a cron trigger would do). Requires `confirm=true`. |

`apply_plan` and `run` are the two tools that change real data in whatever warehouse the project points at. Every other tool is read-only. Both are marked `destructiveHint`/non-`readOnlyHint` in their MCP tool annotations so clients can warn a user before calling them.

One server process is scoped to a single SQLMesh project, set once via `SQLMESH_PROJECT_PATH` (the context is cached for the life of the process). Point a client at multiple projects by running multiple server instances, one per `SQLMESH_PROJECT_PATH`.

### Not yet covered

SQLMesh's `table_diff` and `format` commands aren't exposed as tools yet — planned, not forgotten. Contributions welcome.

## Testing

See [`TEST_CASES.md`](TEST_CASES.md) for a plain-English index of every test case and what it covers, including a real bug the protocol-level tests caught that direct function-call tests couldn't (tool errors getting silently replaced with a generic message unless raised as the SDK's own `ToolError`).

## License

MIT
