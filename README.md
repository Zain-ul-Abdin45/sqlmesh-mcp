# sqlmesh-mcp

[![CI](https://github.com/Zain-ul-Abdin45/sqlmesh-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Zain-ul-Abdin45/sqlmesh-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sqlmesh-mcp.svg)](https://pypi.org/project/sqlmesh-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/sqlmesh-mcp.svg)](https://pypi.org/project/sqlmesh-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MCP server exposing a [SQLMesh](https://github.com/SQLMesh/sqlmesh) project to LLM agents: model metadata, bounded data previews, plan previews, column-level lineage, audits/tests, environment diffs, resources, and reusable prompts.

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
| `get_model_dependencies` | Yes | Direct upstream and downstream model dependencies |
| `fetch_model_data` | Yes | Bounded preview of current model data, capped by `SQLMESH_MAX_RESULT_ROWS` |
| `server_info` | Yes | Project, capability, transport, and safety configuration metadata |
| `start_plan` | Yes | Start a plan preview in the background and return a job ID |
| `start_run` | **No** | Start a scheduled model run in the background; requires `confirm=true` |
| `get_job_status` | Yes | Inspect a background plan or run job |
| `cancel_job` | No | Cancel a queued background job |
| `model_impact` | Yes | Return transitive upstream and downstream model impact |
| `environment_health` | Yes | Summarize environment existence, snapshots, and project drift |
| `audit_history` | Yes | Read recent dataveil audit events without cell values |
| `plan` | Yes | Preview what a plan against an environment would change |
| `apply_plan` | **No** | Apply a previously-previewed plan. Requires `confirm=true`. |
| `lineage` | Yes | Column-level lineage for a model's column |
| `run_audit` | Yes | Run a model's audits |
| `run_test` | Yes | Run a model's unit tests |
| `diff_environment` | Yes | Diff two environments |
| `list_environments` | Yes | List every environment that exists in the project's state |
| `table_diff` | Yes | Compare two tables or environments with structured schema and row differences |
| `format` | Check only by default | Check or format SQLMesh model and audit files; writing requires `confirm=true` |
| `run` | **No** | Execute scheduled/due model runs for an environment (what a cron trigger would do). Requires `confirm=true`. |
| `profile_model` | Yes | Aggregate-only profile of a model's columns (null rates, numeric stats, generalized string format signatures). No raw rows or samples; numeric stats withheld for small or constant columns (see dataveil's Known limits). |
| `propose_cleansing_plan` | Yes | Profile + a local sensitivity classification per column (`PII:EMAIL`, `PII:SSN`, ...) + the operation vocabulary `apply_cleansing_plan` accepts — the contract an agent reasons over to build a plan. |
| `apply_cleansing_plan` | **No** | Validate and apply a data-cleansing plan (mask/drop/impute/... from a closed, pre-tested operation vocabulary — never LLM-generated code). Requires `confirm=true`. |
| `register_cleansing_plan` | Yes | Register a plan for later approval instead of applying it directly. Validates it immediately. Only useful when `DATAVEIL_REQUIRE_PLAN_APPROVAL=true` (see below). |
| `approve_cleansing_plan` | No\* | Mark a registered plan approved by someone. Doesn't touch real data — not read-only (it mutates server-side approval state), not destructive either. |

`apply_plan`, `run`, `start_run`, and `apply_cleansing_plan` change real data in whatever warehouse the project points at. `format` changes project files when called with `check=false`, while `approve_cleansing_plan` and `cancel_job` mutate server-side state without changing warehouse data. Destructive tools are marked `destructiveHint`/non-`readOnlyHint` in their MCP annotations so clients can warn a user before calling them.

`profile_model`/`propose_cleansing_plan`/`apply_cleansing_plan` are powered by [`dataveil`](https://github.com/Zain-ul-Abdin45/dataveil): profiling, PII classification, and plan execution happen locally against aggregate SQL only (never a sample of raw rows), and a cleansing plan can only reference a small, pre-tested operation vocabulary — never SQL or code an LLM wrote itself. See that project's README for its known limits. Note: `apply_cleansing_plan` writes directly to a model's physical snapshot table, outside SQLMesh's own state tracking — a FULL-kind model's next scheduled run/plan apply will rematerialize it and silently undo the cleansing.

### MCP resources and prompts

The server also publishes MCP resources for clients that support browsing context:

| Resource | Description |
|---|---|
| `sqlmesh://models` | JSON metadata for every model |
| `sqlmesh://models/{model_name}` | Rendered SQL and metadata for one model |
| `sqlmesh://lineage/{model_name}/{column}` | Column-level lineage as JSON |

Reusable prompts are available as `explain_model` and `review_plan`. They guide an agent through model analysis and plan review while keeping the final mutation step explicit.

### Optional plan-approval gate

By default, `apply_cleansing_plan` accepts a plan directly (plus `confirm=true`) — the same posture `apply_plan`/`run` already have. Setting `DATAVEIL_REQUIRE_PLAN_APPROVAL=true` adds a second, independent gate for cleansing plans specifically: `apply_cleansing_plan` then refuses a plan passed directly and instead requires a `plan_id` from a plan that was `register_cleansing_plan`'d and then `approve_cleansing_plan`'d — by a different call, potentially a different reviewer. `confirm=true` is still required on top of that approval, not instead of it. A plan is discarded from the registry once applied, so it can't be replayed. Off by default; turn it on once this touches anything with real compliance stakes.

Every call to these three tools is appended to a local JSON-lines audit log (`<project>/.dataveil/audit.jsonl` by default; override with `DATAVEIL_AUDIT_LOG_PATH`) — who/what/when, enough to reconstruct what happened without re-running anything, never a literal cell value.

One server process is scoped to a single SQLMesh project, set once via `SQLMESH_PROJECT_PATH` (the context is cached for the life of the process). Point a client at multiple projects by running multiple server instances, one per `SQLMESH_PROJECT_PATH`.

### Safety and transports

Set `SQLMESH_ALLOWED_ENVIRONMENTS=prod,staging` to restrict plan, run, diff, and table-diff calls to an environment allowlist. When set, mutation calls must include an explicit environment. `SQLMESH_MAX_RESULT_ROWS` defaults to `100` and caps both data previews and table-diff samples. `SQLMESH_PLAN_TTL_SECONDS` defaults to `900`, and `SQLMESH_PLAN_CACHE_SIZE` defaults to `100`; cached plans expire and are bound to the configured project path. Set `SQLMESH_READ_ONLY=true` to disable warehouse, file, and approval-state mutations. Mutating tools still require their individual `confirm=true` gates.

`fetch_model_data` masks columns whose names suggest email, phone, SSN, social-security, or address data. Set `SQLMESH_PREVIEW_COLUMNS` to a comma-separated column allowlist, or pass `columns` per call. Unmasked values require the explicit `SQLMESH_ALLOW_UNMASKED_PREVIEW=true` opt-in and every preview is audit logged.

stdio remains the default transport. For a networked MCP client, run Streamable HTTP with:

```bash
MCP_TRANSPORT=streamable-http \
SQLMESH_PROJECT_PATH=/path/to/project \
MCP_HOST=127.0.0.1 MCP_PORT=8000 \
sqlmesh-mcp
```

The endpoint is available at `http://127.0.0.1:8000/mcp` by default. Set `MCP_AUTH_TOKEN` to protect HTTP requests with `Authorization: Bearer <token>`; non-loopback binding is refused unless a token is configured. Use TLS or an authenticated reverse proxy for production deployment.

For named clients and least-privilege access, use `MCP_AUTH_TOKENS` with a JSON object. Supported scopes are `read`, `mutate`, `approve`, `write`, and `operate`:

```bash
MCP_AUTH_TOKENS='{"analyst":{"token":"read-secret","scopes":["read"]},"deployer":{"token":"deploy-secret","scopes":["read","mutate","operate"]}}' \
MCP_TRANSPORT=streamable-http sqlmesh-mcp
```

Each client may also include an `expires_at` Unix timestamp. `MCP_AUTH_TOKEN` remains supported as a legacy full-scope token. Authenticated client IDs are included in dataveil audit details.

Approval-gated cleansing plans are persisted at `<project>/.dataveil/approvals.json` with restrictive file permissions. Override the location with `DATAVEIL_APPROVALS_PATH` when the project filesystem is not durable.

## Testing

See [`TEST_CASES.md`](TEST_CASES.md) for a plain-English index of every test case and what it covers, including a real bug the protocol-level tests caught that direct function-call tests couldn't (tool errors getting silently replaced with a generic message unless raised as the SDK's own `ToolError`).

## License

MIT
