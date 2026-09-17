"""sqlmesh-mcp: exposes a SQLMesh project to LLM agents via MCP.

Every tool here is read-only except apply_plan, which is the one tool that
changes real data in whatever warehouse the project points at.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from sqlmesh.core.lineage import column_dependencies

from .context import get_context

server = MCPServer("sqlmesh-mcp")

# Plans previewed via `plan` are cached here so `apply_plan` can apply exactly
# what was shown, keyed by Plan.plan_id. Plan objects aren't JSON-serializable
# and re-running plan() at apply time could compute something different if the
# project changed in between preview and apply.
_PLAN_CACHE: dict[str, Any] = {}


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
def list_models() -> list[dict]:
    """List every model in the SQLMesh project with its kind, columns, owner, and description."""
    ctx = get_context()
    return [_model_summary(m) for m in ctx.models.values()]


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_model(model_name: str) -> dict:
    """Full detail for one model: rendered query, columns, kind, owner, tags, description."""
    ctx = get_context()
    model = ctx.get_model(model_name, raise_if_missing=True)
    summary = _model_summary(model)
    summary["query"] = model.render_query_or_raise().sql(dialect=model.dialect, pretty=True)
    return summary


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
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
def apply_plan(plan_id: str, confirm: bool = False) -> dict:
    """Apply a previously-previewed plan. THIS CHANGES REAL DATA in the target warehouse.

    Requires confirm=true. plan_id must come from a plan() call in this same session --
    plans aren't kept across server restarts.
    """
    if not confirm:
        raise ValueError(
            "Refusing to apply without confirm=true -- this changes real data "
            "in the target warehouse."
        )
    p = _PLAN_CACHE.get(plan_id)
    if p is None:
        raise ValueError(
            f"No cached plan with id {plan_id!r}. Call plan() again in this session first."
        )
    ctx = get_context()
    ctx.apply(p)
    del _PLAN_CACHE[plan_id]
    return {"applied": True, "plan_id": plan_id}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def lineage(model_name: str, column: str) -> dict:
    """Column-level lineage: which upstream models/columns does this column depend on."""
    ctx = get_context()
    deps = column_dependencies(ctx, model_name, column)
    return {k: sorted(v) for k, v in deps.items()}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
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
def diff_environment(environment: str) -> dict:
    """Diff the current context against a target environment."""
    ctx = get_context()
    has_diff = ctx.diff(environment=environment)
    return {"environment": environment, "has_diff": has_diff}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
