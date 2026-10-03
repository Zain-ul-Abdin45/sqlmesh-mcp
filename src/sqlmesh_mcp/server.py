"""sqlmesh-mcp: exposes a SQLMesh project to LLM agents via MCP.

Every tool here is read-only except apply_plan, which is the one tool that
changes real data in whatever warehouse the project points at.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import sqlglot
from dataveil.adapters.sqlmesh import SQLMeshAdapter
from dataveil.audit import AuditLog
from dataveil.core.approval import ApprovalError, PlanRegistry, execute_approved_plan
from dataveil.core.classify import classify_table
from dataveil.core.execute import ExecutionError, execute_plan
from dataveil.core.plan import PlanValidationError, operation_vocabulary, validate_plan
from dataveil.core.profile import profile_table
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from sqlmesh.core.lineage import column_dependencies
from sqlmesh.utils.errors import SQLMeshError

from .context import get_context

server = MCPServer("sqlmesh-mcp")

# Plans previewed via `plan` are cached here so `apply_plan` can apply exactly
# what was shown, keyed by Plan.plan_id. Plan objects aren't JSON-serializable
# and re-running plan() at apply time could compute something different if the
# project changed in between preview and apply.
_PLAN_CACHE: dict[str, Any] = {}

# Cleansing plans registered via register_cleansing_plan, pending approval --
# only consulted when DATAVEIL_REQUIRE_PLAN_APPROVAL is set (see
# _require_plan_approval()); apply_cleansing_plan accepts a plan directly
# otherwise, same as today.
_PLAN_REGISTRY = PlanRegistry()


def _require_plan_approval() -> bool:
    return os.environ.get("DATAVEIL_REQUIRE_PLAN_APPROVAL", "").lower() in ("1", "true", "yes")


def _audit_log() -> AuditLog:
    """One JSON-lines audit entry per profile/classify/apply call for the
    dataveil-backed tools below, written next to the SQLMesh project so it's
    easy to find (override with DATAVEIL_AUDIT_LOG_PATH).
    """
    project_path = os.environ.get("SQLMESH_PROJECT_PATH", ".")
    default_path = Path(project_path) / ".dataveil" / "audit.jsonl"
    return AuditLog(os.environ.get("DATAVEIL_AUDIT_LOG_PATH", str(default_path)))


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
        except (SQLMeshError, PlanValidationError, ExecutionError, ValueError, ApprovalError) as e:
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
def plan(environment: str | None = None, select_models: list[str] | None = None) -> dict:
    """Preview what a plan against an environment would change. Does not apply anything.

    Returns a plan_id -- pass it to apply_plan to actually apply this exact plan.
    """
    ctx = get_context()
    p = ctx.plan(
        environment=environment,
        select_models=select_models,
        no_prompts=True,
        auto_apply=False,
    )
    _PLAN_CACHE[p.plan_id] = p
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
    if not confirm:
        raise ToolError(
            "Refusing to apply without confirm=true -- this changes real data "
            "in the target warehouse."
        )
    p = _PLAN_CACHE.get(plan_id)
    if p is None:
        raise ToolError(
            f"No cached plan with id {plan_id!r}. Call plan() again in this session first."
        )
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
    _audit_log().record("profile", model_name, {"row_count": result["row_count"]})
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
    _audit_log().record(
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
    pending = _PLAN_REGISTRY.approve(plan_id, approved_by=approved_by)
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
    else:
        if plan is None:
            raise ToolError(
                "apply_cleansing_plan requires 'plan' (DATAVEIL_REQUIRE_PLAN_APPROVAL is not set)"
            )
        results = execute_plan(adapter, model_name, plan, confirm=True)

    _audit_log().record(
        "execute_plan",
        model_name,
        {"steps": [{"operation": r["operation"], "column": r["column"]} for r in results]},
    )
    return {"model": model_name, "applied_steps": results}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
