"""sqlmesh-mcp: exposes a SQLMesh project to LLM agents via MCP.

Read-only metadata tools are separated from tools that can change project files,
server state, or warehouse data.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import functools
import hmac
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import sqlglot
from dataveil.adapters.sqlmesh import SQLMeshAdapter
from dataveil.audit import AuditLog
from dataveil.core.approval import ApprovalError, PendingPlan, PlanRegistry, execute_approved_plan
from dataveil.core.classify import classify_table
from dataveil.core.execute import ExecutionError, execute_plan
from dataveil.core.plan import PlanValidationError, operation_vocabulary, validate_plan
from dataveil.core.profile import profile_table
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from sqlmesh.core.lineage import column_dependencies
from sqlmesh.utils.errors import SQLMeshError

from .context import ProjectNotConfiguredError, get_context

server = MCPServer("sqlmesh-mcp")

# Plans previewed via `plan` are cached here so `apply_plan` can apply exactly
# what was shown, keyed by Plan.plan_id. Plan objects aren't JSON-serializable
# and re-running plan() at apply time could compute something different if the
# project changed in between preview and apply.
_PLAN_CACHE: dict[str, dict[str, Any]] = {}

# Cleansing plans registered via register_cleansing_plan, pending approval --
# only consulted when DATAVEIL_REQUIRE_PLAN_APPROVAL is set (see
# _require_plan_approval()); apply_cleansing_plan accepts a plan directly
# otherwise, same as today.
_PLAN_REGISTRY = PlanRegistry()
_APPROVAL_LOCK = threading.Lock()

_JOB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_AUTH_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "sqlmesh_mcp_auth_context", default=None
)


def _require_plan_approval() -> bool:
    return os.environ.get("DATAVEIL_REQUIRE_PLAN_APPROVAL", "").lower() in ("1", "true", "yes")


def _read_only_mode() -> bool:
    return os.environ.get("SQLMESH_READ_ONLY", "").lower() in ("1", "true", "yes")


def _require_writable() -> None:
    if _read_only_mode():
        raise ToolError(
            "This server is in SQLMESH_READ_ONLY mode and refuses operations that mutate "
            "warehouse, project files, or approval state."
        )


def _require_scope(scope: str) -> None:
    context = _AUTH_CONTEXT.get()
    if context is not None and scope not in context["scopes"]:
        raise ToolError(f"Authenticated client {context['client_id']!r} lacks scope {scope!r}.")


def _auth_clients() -> dict[str, dict[str, Any]]:
    allowed_scopes = {"read", "mutate", "approve", "write", "operate"}
    configured = os.environ.get("MCP_AUTH_TOKENS")
    if configured:
        try:
            records = json.loads(configured)
        except json.JSONDecodeError as error:
            raise ValueError("MCP_AUTH_TOKENS must be valid JSON.") from error
        if not isinstance(records, dict):
            raise ValueError("MCP_AUTH_TOKENS must be a JSON object keyed by client ID.")
        clients = {}
        for client_id, record in records.items():
            if not isinstance(record, dict) or not isinstance(record.get("token"), str):
                raise ValueError(f"MCP_AUTH_TOKENS entry {client_id!r} needs a string token.")
            scopes = record.get("scopes", ["read"])
            if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
                raise ValueError(f"MCP_AUTH_TOKENS entry {client_id!r} needs a scope list.")
            unknown_scopes = set(scopes) - allowed_scopes
            if unknown_scopes:
                raise ValueError(
                    f"MCP_AUTH_TOKENS entry {client_id!r} has unknown scopes: "
                    f"{', '.join(sorted(unknown_scopes))}."
                )
            expires_at = record.get("expires_at")
            if expires_at is not None:
                try:
                    expires_at = float(expires_at)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"MCP_AUTH_TOKENS entry {client_id!r} needs a numeric expires_at."
                    ) from error
            clients[client_id] = {
                "token": record["token"],
                "scopes": set(scopes),
                "expires_at": expires_at,
            }
        return clients
    token = os.environ.get("MCP_AUTH_TOKEN")
    if token:
        return {
            "default": {
                "token": token,
                "scopes": {"read", "mutate", "approve", "write", "operate"},
            }
        }
    return {}


def _record_audit(kind: str, table: str, details: dict[str, Any]) -> None:
    context = _AUTH_CONTEXT.get()
    enriched = dict(details)
    if context is not None:
        enriched["client_id"] = context["client_id"]
    _audit_log().record(kind, table, enriched)


def _job_snapshot(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise ToolError(f"No job with id {job_id!r}.")
        return {key: value for key, value in job.items() if key != "future"}


def _run_job(job_id: str, operation: Any) -> None:
    with _JOB_LOCK:
        _JOBS[job_id]["status"] = "running"
        _JOBS[job_id]["started_at"] = time.time()
    try:
        result = operation()
    except Exception as error:
        with _JOB_LOCK:
            _JOBS[job_id].update(
                {"status": "failed", "error": str(error), "finished_at": time.time()}
            )
    else:
        with _JOB_LOCK:
            _JOBS[job_id].update(
                {"status": "completed", "result": result, "finished_at": time.time()}
            )


def _submit_job(kind: str, operation: Any) -> str:
    job_id = str(uuid.uuid4())
    auth_context = _AUTH_CONTEXT.get()

    def operation_with_identity() -> Any:
        token = _AUTH_CONTEXT.set(auth_context)
        try:
            return operation()
        finally:
            _AUTH_CONTEXT.reset(token)

    with _JOB_LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": time.time(),
        }
        future = _JOB_EXECUTOR.submit(_run_job, job_id, operation_with_identity)
        _JOBS[job_id]["future"] = future
    return job_id


def _allowed_environments() -> set[str] | None:
    configured = os.environ.get("SQLMESH_ALLOWED_ENVIRONMENTS")
    if not configured:
        return None
    return {name.strip() for name in configured.split(",") if name.strip()}


def _check_environment(environment: str | None) -> None:
    allowed = _allowed_environments()
    if environment and allowed is not None and environment not in allowed:
        raise ToolError(
            f"Environment {environment!r} is not allowed by SQLMESH_ALLOWED_ENVIRONMENTS."
        )


def _require_mutation_environment(environment: str | None) -> None:
    if _allowed_environments() is not None and not environment:
        raise ToolError(
            "An explicit environment is required when SQLMESH_ALLOWED_ENVIRONMENTS is set."
        )
    _check_environment(environment)


def _plan_ttl_seconds() -> int:
    try:
        value = int(os.environ.get("SQLMESH_PLAN_TTL_SECONDS", "900"))
    except ValueError as e:
        raise ToolError("SQLMESH_PLAN_TTL_SECONDS must be a positive integer.") from e
    if value < 1:
        raise ToolError("SQLMESH_PLAN_TTL_SECONDS must be a positive integer.")
    return value


def _plan_cache_limit() -> int:
    try:
        value = int(os.environ.get("SQLMESH_PLAN_CACHE_SIZE", "100"))
    except ValueError as e:
        raise ToolError("SQLMESH_PLAN_CACHE_SIZE must be a positive integer.") from e
    if value < 1:
        raise ToolError("SQLMESH_PLAN_CACHE_SIZE must be a positive integer.")
    return value


def _max_rows() -> int:
    try:
        value = int(os.environ.get("SQLMESH_MAX_RESULT_ROWS", "100"))
    except ValueError as e:
        raise ToolError("SQLMESH_MAX_RESULT_ROWS must be a positive integer.") from e
    if value < 1:
        raise ToolError("SQLMESH_MAX_RESULT_ROWS must be a positive integer.")
    return value


def _preview_columns(model: Any, columns: list[str] | None) -> list[str]:
    available = list(model.columns_to_types or {})
    requested = columns
    configured = os.environ.get("SQLMESH_PREVIEW_COLUMNS")
    if requested is None and configured:
        requested = [column.strip() for column in configured.split(",") if column.strip()]
    if requested is None:
        return available
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ToolError(f"Unknown preview columns for {model.name}: {', '.join(unknown)}")
    return requested


def _mask_preview_rows(rows: list[dict], columns: list[str], enabled: bool) -> list[dict]:
    if not enabled:
        return rows
    sensitive_markers = ("email", "phone", "ssn", "social_security", "address")
    sensitive_columns = {
        column
        for column in columns
        if any(marker in column.lower() for marker in sensitive_markers)
    }
    for row in rows:
        for column in sensitive_columns:
            if row.get(column) is not None:
                row[column] = "[REDACTED]"
    return rows


def _audit_log() -> AuditLog:
    """One JSON-lines audit entry per profile/classify/apply call for the
    dataveil-backed tools below, written next to the SQLMesh project so it's
    easy to find (override with DATAVEIL_AUDIT_LOG_PATH).
    """
    project_path = os.environ.get("SQLMESH_PROJECT_PATH", ".")
    default_path = Path(project_path) / ".dataveil" / "audit.jsonl"
    return AuditLog(os.environ.get("DATAVEIL_AUDIT_LOG_PATH", str(default_path)))


def _approval_path() -> Path:
    project_path = os.environ.get("SQLMESH_PROJECT_PATH", ".")
    return Path(
        os.environ.get(
            "DATAVEIL_APPROVALS_PATH",
            str(Path(project_path) / ".dataveil" / "approvals.json"),
        )
    )


def _load_approvals() -> None:
    path = _approval_path()
    if not path.exists():
        return
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError(f"Could not read approval records: {error}") from error
    if not isinstance(records, list):
        raise ToolError("Approval record file must contain a JSON list.")
    with _APPROVAL_LOCK:
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                pending = PendingPlan(**record)
            except (TypeError, ValueError) as error:
                raise ToolError(f"Invalid approval record: {error}") from error
            _PLAN_REGISTRY._plans.setdefault(pending.id, pending)


def _persist_approvals() -> None:
    path = _approval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPROVAL_LOCK:
        records = [pending.to_dict() for pending in _PLAN_REGISTRY._plans.values()]
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(path)


def _translate_errors(fn):
    """Without this, the MCP SDK treats any exception that isn't a ToolError as
    a crash: the agent sees only the generic "Error executing tool <name>" and
    the real message (e.g. SQLMesh's "Apply a plan first") is dropped, visible
    only in server-side logs. SQLMeshError covers every error the underlying
    library itself raises intentionally, so translating it is always safe to
    show the agent -- it's exactly the informative half of the message.
    PlanValidationError/ExecutionError are dataveil's equivalent for the
    cleansing-plan tools below (e.g. "unknown operation", "missing required
    param"); ValueError is what dataveil's SQLMeshAdapter itself raises for
    its own intentional errors (e.g. "apply a plan first"); ApprovalError is
    the optional plan-approval gate's equivalent (e.g. "has not been
    approved yet") -- same reasoning applies to all of them.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (
            SQLMeshError,
            PlanValidationError,
            ExecutionError,
            ProjectNotConfiguredError,
            ValueError,
            ApprovalError,
        ) as e:
            raise ToolError(str(e)) from e

    return wrapper


def _snapshot_model_name(snapshot_id: Any) -> str:
    """SnapshotId.name is a quoted, catalog-qualified identifier (e.g.
    '"db"."sqlmesh_example"."seed_model"') -- normalize to the plain
    'schema.model' form every other tool here uses.
    """
    t = sqlglot.exp.to_table(snapshot_id.name)
    return f"{t.db}.{t.name}" if t.db else t.name


def _model_summary(model: Any) -> dict:
    return {
        "name": model.name,
        "kind": str(model.kind.name),
        "description": model.description,
        "owner": model.owner,
        "tags": list(model.tags or []),
        "columns": {col: str(dtype) for col, dtype in (model.columns_to_types or {}).items()},
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def list_models() -> list[dict]:
    """List every model in the SQLMesh project with its kind, columns, owner, and description."""
    ctx = get_context()
    return [_model_summary(m) for m in ctx.models.values()]


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def get_model(model_name: str) -> dict:
    """Full detail for one model: rendered query, columns, kind, owner, tags, description."""
    ctx = get_context()
    model = ctx.get_model(model_name, raise_if_missing=True)
    summary = _model_summary(model)
    summary["query"] = model.render_query_or_raise().sql(dialect=model.dialect, pretty=True)
    return summary


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def get_model_dependencies(model_name: str) -> dict:
    """Return the direct upstream and downstream model dependencies."""
    ctx = get_context()
    model = ctx.get_model(model_name, raise_if_missing=True)
    upstream = sorted(str(sqlglot.exp.to_table(name)) for name in model.depends_on)
    downstream = sorted(
        other.name
        for other in ctx.models.values()
        if model.name in {str(sqlglot.exp.to_table(name)) for name in other.depends_on}
    )
    return {"model": model.name, "upstream": upstream, "downstream": downstream}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def fetch_model_data(
    model_name: str,
    limit: int = 20,
    columns: list[str] | None = None,
    mask_sensitive: bool = True,
) -> dict:
    """Fetch a bounded, optionally column-filtered preview of model data."""
    maximum = _max_rows()
    if limit < 1 or limit > maximum:
        raise ToolError(f"limit must be between 1 and {maximum}.")
    if not mask_sensitive and os.environ.get("SQLMESH_ALLOW_UNMASKED_PREVIEW", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        raise ToolError(
            "Unmasked previews require SQLMESH_ALLOW_UNMASKED_PREVIEW=true."
        )
    ctx = get_context()
    model = ctx.get_model(model_name, raise_if_missing=True)
    selected_columns = _preview_columns(model, columns)
    table = sqlglot.exp.to_table(model.name).sql(dialect=model.dialect)
    selected_sql = ", ".join(
        sqlglot.exp.column(column).sql(dialect=model.dialect) for column in selected_columns
    )
    frame = ctx.fetchdf(f"SELECT {selected_sql} FROM {table} LIMIT {limit}")
    rows = _mask_preview_rows(frame.to_dict(orient="records"), selected_columns, mask_sensitive)
    _record_audit(
        "preview",
        model_name,
        {"row_count": len(rows), "columns": selected_columns, "masked": mask_sensitive},
    )
    return {
        "model": model.name,
        "limit": limit,
        "columns": selected_columns,
        "masked": mask_sensitive,
        "rows": rows,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def server_info() -> dict:
    """Return server, project, safety, and capability metadata."""
    ctx = get_context()
    return {
        "server": "sqlmesh-mcp",
        "project_path": os.environ.get("SQLMESH_PROJECT_PATH"),
        "model_count": len(ctx.models),
        "allowed_environments": sorted(_allowed_environments() or []),
        "max_result_rows": _max_rows(),
        "transport": os.environ.get("MCP_TRANSPORT", "stdio"),
        "read_only": _read_only_mode(),
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def start_plan(environment: str | None = None, select_models: list[str] | None = None) -> dict:
    """Start a plan preview in the background and return a job ID."""
    _require_mutation_environment(environment)
    job_id = _submit_job("plan", lambda: plan(environment, select_models))
    return {"job_id": job_id, "kind": "plan", "status": "queued"}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@_translate_errors
def start_run(
    environment: str | None = None,
    confirm: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Start a scheduled model run in the background. Requires confirm=true."""
    _require_scope("mutate")
    _require_writable()
    _require_mutation_environment(environment)
    if not confirm:
        raise ToolError("Refusing to start a run without confirm=true -- this changes real data.")
    job_id = _submit_job(
        "run",
        lambda: run(environment=environment, confirm=True, start=start, end=end),
    )
    return {"job_id": job_id, "kind": "run", "status": "queued"}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def get_job_status(job_id: str) -> dict:
    """Return the current status and result of an asynchronous plan or run."""
    return _job_snapshot(job_id)


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
@_translate_errors
def cancel_job(job_id: str) -> dict:
    """Cancel a queued job. Running SQLMesh operations cannot be interrupted safely."""
    _require_scope("operate")
    _require_writable()
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise ToolError(f"No job with id {job_id!r}.")
        if job["status"] != "queued":
            return _job_snapshot(job_id)
        cancelled = job["future"].cancel()
        if cancelled:
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
    return _job_snapshot(job_id)


def _dependency_names(model: Any) -> set[str]:
    names = set()
    for dependency in model.depends_on:
        table = sqlglot.exp.to_table(dependency)
        names.add(f"{table.db}.{table.name}" if table.db else table.name)
    return names


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def model_impact(model_name: str) -> dict:
    """Return all transitive upstream and downstream models affected by a change."""
    ctx = get_context()
    ctx.get_model(model_name, raise_if_missing=True)
    models = {model.name: model for model in ctx.models.values()}
    upstream: set[str] = set()
    frontier = [model_name]
    while frontier:
        current = frontier.pop()
        for dependency in _dependency_names(models[current]):
            if dependency in models and dependency not in upstream:
                upstream.add(dependency)
                frontier.append(dependency)
    downstream: set[str] = set()
    frontier = [model_name]
    while frontier:
        current = frontier.pop()
        for name, model in models.items():
            if current in _dependency_names(model) and name not in downstream:
                downstream.add(name)
                frontier.append(name)
    return {
        "model": model_name,
        "upstream": sorted(upstream),
        "downstream": sorted(downstream - {model_name}),
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def environment_health(environment: str) -> dict:
    """Summarize environment existence, snapshots, and current project drift."""
    _check_environment(environment)
    ctx = get_context()
    env = ctx.state_reader.get_environment(environment)
    if env is None:
        return {"environment": environment, "exists": False, "healthy": False}
    try:
        has_diff = ctx.diff(environment=environment)
        diff_error = None
    except Exception as error:
        has_diff = None
        diff_error = str(error)
    return {
        "environment": environment,
        "exists": True,
        "healthy": diff_error is None and not has_diff,
        "has_diff": has_diff,
        "error": diff_error,
        "snapshot_count": len(env.snapshots),
        "finalized_ts": env.finalized_ts,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def audit_history(limit: int = 20) -> list[dict]:
    """Return recent dataveil audit events without exposing cell values."""
    maximum = _max_rows()
    if limit < 1 or limit > maximum:
        raise ToolError(f"limit must be between 1 and {maximum}.")
    return [event.to_dict() for event in _audit_log().read_all()[-limit:]]


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def plan(environment: str | None = None, select_models: list[str] | None = None) -> dict:
    """Preview what a plan against an environment would change. Does not apply anything.

    Returns a plan_id -- pass it to apply_plan to actually apply this exact plan.
    """
    _require_mutation_environment(environment)
    ctx = get_context()
    p = ctx.plan(
        environment=environment,
        select_models=select_models,
        no_prompts=True,
        auto_apply=False,
    )
    if len(_PLAN_CACHE) >= _plan_cache_limit():
        oldest_plan_id = min(_PLAN_CACHE, key=lambda plan_id: _PLAN_CACHE[plan_id]["created_at"])
        del _PLAN_CACHE[oldest_plan_id]
    _PLAN_CACHE[p.plan_id] = {
        "plan": p,
        "created_at": time.time(),
        "project_path": str(Path(os.environ.get("SQLMESH_PROJECT_PATH", ".")).resolve()),
        "environment": p.environment_naming_info.name,
    }
    diff = p.context_diff
    return {
        "plan_id": p.plan_id,
        "environment": p.environment_naming_info.name,
        "has_changes": diff.has_changes,
        "requires_backfill": p.requires_backfill,
        "added_models": sorted(_snapshot_model_name(s) for s in diff.added),
        "removed_models": sorted(_snapshot_model_name(s) for s in diff.removed_snapshots),
        "modified_models": sorted(diff.modified_snapshots),
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@_translate_errors
def apply_plan(plan_id: str, confirm: bool = False) -> dict:
    """Apply a previously-previewed plan. THIS CHANGES REAL DATA in the target warehouse.

    Requires confirm=true. plan_id must come from a plan() call in this same session --
    plans aren't kept across server restarts.
    """
    _require_scope("mutate")
    _require_writable()
    if not confirm:
        raise ToolError(
            "Refusing to apply without confirm=true -- this changes real data "
            "in the target warehouse."
        )
    entry = _PLAN_CACHE.get(plan_id)
    if entry is None:
        raise ToolError(
            f"No cached plan with id {plan_id!r}. Call plan() again in this session first."
        )
    if time.time() - entry["created_at"] > _plan_ttl_seconds():
        del _PLAN_CACHE[plan_id]
        raise ToolError(f"Cached plan {plan_id!r} has expired. Call plan() again first.")
    current_project = str(Path(os.environ.get("SQLMESH_PROJECT_PATH", ".")).resolve())
    if entry["project_path"] != current_project:
        raise ToolError("Cached plan belongs to a different SQLMESH_PROJECT_PATH.")
    _require_mutation_environment(entry["environment"])
    p = entry["plan"]
    ctx = get_context()
    ctx.apply(p)
    del _PLAN_CACHE[plan_id]
    return {"applied": True, "plan_id": plan_id}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def lineage(model_name: str, column: str) -> dict:
    """Column-level lineage: which upstream models/columns does this column depend on."""
    ctx = get_context()
    deps = column_dependencies(ctx, model_name, column)
    return {k: sorted(v) for k, v in deps.items()}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def run_audit(
    model_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Run audits for a model (or all models if omitted). start/end bound the data checked."""
    ctx = get_context()
    passed = ctx.audit(start=start, end=end, models=[model_name] if model_name else None)
    return {"passed": passed, "model": model_name}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def run_test(model_name: str | None = None) -> dict:
    """Run unit tests for a model (or all tests if omitted)."""
    ctx = get_context()
    result = ctx.test(model_names=[model_name] if model_name else None)
    return {
        "success": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": [str(f[0]) for f in result.failures],
        "errors": [str(e[0]) for e in result.errors],
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def diff_environment(environment: str) -> dict:
    """Diff the current context against a target environment."""
    _check_environment(environment)
    ctx = get_context()
    has_diff = ctx.diff(environment=environment)
    return {"environment": environment, "has_diff": has_diff}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def list_environments() -> list[dict]:
    """List every environment that exists in this project's state (e.g. prod, dev, ...)."""
    ctx = get_context()
    envs = ctx.state_reader.get_environments()
    return [
        {
            "name": e.name,
            "plan_id": e.plan_id,
            "start_at": str(e.start_at) if e.start_at else None,
            "end_at": str(e.end_at) if e.end_at else None,
            "finalized_ts": e.finalized_ts,
            "expiration_ts": e.expiration_ts,
        }
        for e in envs
    ]


def _table_diff_summary(table_diff: Any) -> dict:
    schema_diff = table_diff.schema_diff()
    row_diff = table_diff.row_diff()
    return {
        "model": table_diff.model_name,
        "source": str(table_diff.source),
        "target": str(table_diff.target),
        "key_columns": table_diff.key_columns[2],
        "schema": {
            "added": {name: str(dtype) for name, dtype in schema_diff.added},
            "removed": {name: str(dtype) for name, dtype in schema_diff.removed},
            "modified": {
                name: {"source": str(source), "target": str(target)}
                for name, (source, target) in schema_diff.modified.items()
            },
        },
        "rows": {
            "source_count": row_diff.source_count,
            "target_count": row_diff.target_count,
            "join_count": row_diff.join_count,
            "full_match_count": row_diff.full_match_count,
            "partial_match_count": row_diff.partial_match_count,
            "full_match_pct": row_diff.full_match_pct,
            "partial_match_pct": row_diff.partial_match_pct,
            "column_stats": row_diff.column_stats.to_dict(orient="records"),
        },
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def table_diff(
    source: str,
    target: str,
    select_models: list[str] | None = None,
    on: list[str] | None = None,
    skip_columns: list[str] | None = None,
    where: str | None = None,
    limit: int = 20,
) -> dict:
    """Compare two tables or environments and return structured schema/row differences.

    When comparing environments, pass select_models to choose the models to compare.
    SQLMesh's console output is suppressed because MCP clients receive the JSON result.
    """
    _check_environment(source)
    _check_environment(target)
    maximum = _max_rows()
    if limit < 1 or limit > maximum:
        raise ToolError(f"limit must be between 1 and {maximum}.")
    ctx = get_context()
    diffs = ctx.table_diff(
        source=source,
        target=target,
        select_models=select_models,
        on=on,
        skip_columns=skip_columns,
        where=where,
        limit=limit,
        show=False,
        show_sample=False,
    )
    return {
        "source": source,
        "target": target,
        "diffs": [_table_diff_summary(diff) for diff in diffs],
    }


@server.resource(
    "sqlmesh://models",
    name="models",
    description="All SQLMesh model metadata in the configured project.",
    mime_type="application/json",
)
def models_resource() -> str:
    return json.dumps(list_models(), default=str)


@server.resource(
    "sqlmesh://models/{model_name}",
    name="model",
    description="Rendered SQL and metadata for one SQLMesh model.",
    mime_type="application/json",
)
def model_resource(model_name: str) -> str:
    return json.dumps(get_model(model_name), default=str)


@server.resource(
    "sqlmesh://lineage/{model_name}/{column}",
    name="column-lineage",
    description="Column-level lineage for a SQLMesh model column.",
    mime_type="application/json",
)
def lineage_resource(model_name: str, column: str) -> str:
    return json.dumps(lineage(model_name, column), default=str)


@server.prompt(
    name="explain_model",
    description="Explain a model's purpose, inputs, outputs, and lineage.",
)
def explain_model_prompt(model_name: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                f"Explain the SQLMesh model {model_name}. Read the model resource, "
                "summarize its business purpose, upstream/downstream dependencies, "
                "grain, important columns, and potential data-quality risks."
            ),
        }
    ]


@server.prompt(
    name="review_plan",
    description="Review a SQLMesh plan preview before it is applied.",
)
def review_plan_prompt(environment: str = "prod") -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                f"Create and review a SQLMesh plan for environment {environment}. "
                "Explain every added, removed, or modified model, identify backfills, "
                "and stop before applying anything."
            ),
        }
    ]


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
@_translate_errors
def format(
    check: bool = True,
    paths: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """Check or format SQLMesh model and audit files.

    The default check mode is read-only. Writing formatted files requires
    confirm=true and is never performed accidentally by an agent.
    """
    if not check and not confirm:
        raise ToolError(
            "Refusing to format files without confirm=true -- this changes project files."
        )
    if not check:
        _require_scope("write")
        _require_writable()
    ctx = get_context()
    formatted = ctx.format(check=check, paths=tuple(paths) if paths else None)
    return {
        "check": check,
        "formatted": not check,
        "needs_formatting": not formatted if check else False,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@_translate_errors
def run(
    environment: str | None = None,
    confirm: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Execute scheduled/due model runs for an environment. THIS CHANGES REAL DATA.

    Distinct from plan/apply_plan: this runs already-promoted models for their
    due intervals (what a cron trigger would do), rather than previewing or
    promoting structural changes. Requires confirm=true.
    """
    _require_scope("mutate")
    _require_writable()
    _require_mutation_environment(environment)
    if not confirm:
        raise ToolError(
            "Refusing to run without confirm=true -- this changes real data "
            "in the target warehouse."
        )
    ctx = get_context()
    status = ctx.run(environment=environment, start=start, end=end)
    return {"status": status.name, "environment": environment}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def profile_model(model_name: str) -> dict:
    """Aggregate-only profile of a model's columns: null rates, distinct
    counts, numeric stats, generalized string format signatures (e.g.
    'ddd-dd-dddd'). Returns no raw rows or samples; see dataveil's
    README for its known limits.

    The model must already be applied (see plan/apply_plan) -- dataveil reads
    from its current physical snapshot table.
    """
    ctx = get_context()
    adapter = SQLMeshAdapter(ctx)
    result = profile_table(adapter, model_name).to_dict()
    _record_audit("profile", model_name, {"row_count": result["row_count"]})
    return result


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def propose_cleansing_plan(model_name: str) -> dict:
    """Everything an agent needs to propose a data-cleansing plan for a model:
    the aggregate-only profile, a local sensitivity classification per column
    (PII:EMAIL, PII:SSN, PII:PHONE, ... or "none", each with a match_rate),
    and the closed operation vocabulary apply_cleansing_plan will accept.

    This tool does not call an LLM or propose a plan itself -- it's the
    documented contract an agent (you) reasons over to build one. A plan is a
    list of {"operation", "column", "params", "rationale"} objects;
    "operation" must be a key in operation_vocabulary, and only the params
    listed for it are accepted. Pass the finished plan to
    apply_cleansing_plan.
    """
    ctx = get_context()
    adapter = SQLMeshAdapter(ctx)
    profile = profile_table(adapter, model_name)
    classifications = classify_table(adapter, model_name)
    _record_audit(
        "classify",
        model_name,
        {"tags": {c.column: c.tag for c in classifications}},
    )
    return {
        "model": model_name,
        "profile": profile.to_dict(),
        "classifications": [c.to_dict() for c in classifications],
        "operation_vocabulary": operation_vocabulary(),
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
@_translate_errors
def register_cleansing_plan(model_name: str, plan: list[dict]) -> dict:
    """Register a cleansing plan for later approval, instead of applying it
    directly. Only useful when this server requires plan approval
    (DATAVEIL_REQUIRE_PLAN_APPROVAL=true) -- otherwise apply_cleansing_plan
    already accepts a plan directly and this tool isn't needed.

    Validates the plan against the operation vocabulary and the model's
    schema immediately -- a reviewer should never approve something that
    would fail at execution time. Read-only: nothing is applied here.

    Returns a plan_id; pass it to approve_cleansing_plan, then to
    apply_cleansing_plan.
    """
    _require_scope("approve")
    _require_writable()
    _load_approvals()
    ctx = get_context()
    adapter = SQLMeshAdapter(ctx)
    validate_plan(plan, adapter.get_schema(model_name))
    pending = _PLAN_REGISTRY.register(model_name, plan)
    return {"plan_id": pending.id, "model": model_name, "plan": plan, "approved": False}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
@_translate_errors
def approve_cleansing_plan(plan_id: str, approved_by: str) -> dict:
    """Mark a plan registered via register_cleansing_plan as approved.

    This only records who approved it -- apply_cleansing_plan still
    requires confirm=true on top of this. Approval and confirm=true are two
    separate gates, not a substitute for each other.
    """
    _require_scope("approve")
    _require_writable()
    _load_approvals()
    pending = _PLAN_REGISTRY.approve(plan_id, approved_by=approved_by)
    _persist_approvals()
    return {
        "plan_id": pending.id,
        "model": pending.table,
        "approved": pending.approved,
        "approved_by": pending.approved_by,
        "approved_at": pending.approved_at,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@_translate_errors
def apply_cleansing_plan(
    model_name: str,
    plan: list[dict] | None = None,
    plan_id: str | None = None,
    confirm: bool = False,
) -> dict:
    """Validate and apply a data-cleansing plan. THIS CHANGES REAL DATA in
    the model's current physical snapshot table. Requires confirm=true.

    Default mode: pass `plan` directly, built from propose_cleansing_plan's
    output -- validated against the operation vocabulary and the model's
    schema before any step runs, same as always.

    If this server requires plan approval (DATAVEIL_REQUIRE_PLAN_APPROVAL=
    true): pass `plan_id` instead, from a plan that was registered
    (register_cleansing_plan) and then approved (approve_cleansing_plan) by
    someone else. confirm=true is still required on top of that approval,
    not instead of it.

    Note: this writes directly to the model's physical table, outside
    SQLMesh's own state tracking. A FULL-kind model is fully rematerialized
    on its next scheduled run/plan apply, which will silently undo the
    cleansing -- it was never folded into the model's SQL definition.
    Re-apply after any such rerun if the cleansing still applies.
    """
    _require_scope("mutate")
    _require_writable()
    _load_approvals()
    if not confirm:
        raise ToolError(
            "Refusing to apply without confirm=true -- this changes real data "
            "in the model's physical table."
        )
    ctx = get_context()
    adapter = SQLMeshAdapter(ctx)

    if _require_plan_approval():
        if plan_id is None:
            raise ToolError(
                "This server requires plan approval (DATAVEIL_REQUIRE_PLAN_APPROVAL=true) -- "
                "call register_cleansing_plan() then approve_cleansing_plan() first, and pass "
                "plan_id here instead of plan."
            )
        pending = _PLAN_REGISTRY.get(plan_id)
        if pending is None:
            raise ToolError(f"no registered plan with id {plan_id!r}")
        if pending.table != model_name:
            raise ToolError(
                f"plan {plan_id!r} was registered for {pending.table!r}, not {model_name!r}"
            )
        results = execute_approved_plan(adapter, _PLAN_REGISTRY, plan_id, confirm=True)
        _PLAN_REGISTRY.discard(plan_id)
        _persist_approvals()
    else:
        if plan is None:
            raise ToolError(
                "apply_cleansing_plan requires 'plan' (DATAVEIL_REQUIRE_PLAN_APPROVAL is not set)"
            )
        results = execute_plan(adapter, model_name, plan, confirm=True)

    _record_audit(
        "execute_plan",
        model_name,
        {"steps": [{"operation": r["operation"], "column": r["column"]} for r in results]},
    )
    return {"model": model_name, "applied_steps": results}


class _BearerAuthMiddleware:
    def __init__(self, app: Any, clients: dict[str, dict[str, Any]]):
        self.app = app
        self.clients = clients

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"authorization"),
            b"",
        )
        authenticated = None
        for client_id, client in self.clients.items():
            expected = f"Bearer {client['token']}".encode("utf-8")
            if hmac.compare_digest(authorization, expected):
                expires_at = client.get("expires_at")
                if expires_at is None or float(expires_at) > time.time():
                    authenticated = {"client_id": client_id, "scopes": client["scopes"]}
                break
        if authenticated is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", b"Bearer")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Unauthorized",
                }
            )
            return
        token = _AUTH_CONTEXT.set(authenticated)
        try:
            await self.app(scope, receive, send)
        finally:
            _AUTH_CONTEXT.reset(token)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        clients = _auth_clients()
        if host not in loopback_hosts and not clients:
            raise ValueError(
                "Remote HTTP binding requires MCP_AUTH_TOKEN or MCP_AUTH_TOKENS. "
                "Use loopback binding for "
                "local unauthenticated development."
            )
        app = server.streamable_http_app(
            host=host,
            streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
        )
        if clients:
            app = _BearerAuthMiddleware(app, clients)
        import uvicorn

        uvicorn.run(app, host=host, port=int(os.environ.get("MCP_PORT", "8000")))
    else:
        raise ValueError("MCP_TRANSPORT must be 'stdio' or 'streamable-http'.")


if __name__ == "__main__":
    main()
