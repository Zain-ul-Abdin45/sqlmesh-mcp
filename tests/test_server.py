"""Integration tests against the real demo SQLMesh project in examples/demo_project.

These call the underlying tool functions directly (not through the MCP
protocol layer) to keep the tests fast and focused on our own logic --
the MCP transport itself is the SDK's responsibility, not ours.
"""

from pathlib import Path

import pytest

DEMO_PROJECT = Path(__file__).parent.parent / "examples" / "demo_project"


@pytest.fixture(autouse=True)
def project_env(monkeypatch):
    monkeypatch.setenv("SQLMESH_PROJECT_PATH", str(DEMO_PROJECT))
    # get_context() is lru_cache'd; make sure each test gets a fresh Context
    # bound to the env var set above rather than a stale cached one.
    from sqlmesh_mcp.context import get_context

    get_context.cache_clear()
    yield
    get_context.cache_clear()


def test_list_models_returns_the_demo_project_models():
    from sqlmesh_mcp.server import list_models

    models = list_models()
    names = {m["name"] for m in models}
    assert "sqlmesh_example.full_model" in names
    assert "sqlmesh_example.incremental_model" in names
    assert "sqlmesh_example.seed_model" in names

    full_model = next(m for m in models if m["name"] == "sqlmesh_example.full_model")
    assert full_model["kind"] == "FULL"
    assert "num_orders" in full_model["columns"]


def test_get_model_includes_rendered_query():
    from sqlmesh_mcp.server import get_model

    model = get_model("sqlmesh_example.incremental_model")
    assert model["kind"] == "INCREMENTAL_BY_TIME_RANGE"
    assert "seed_model" in model["query"]


def test_get_model_raises_for_unknown_model():
    from sqlmesh_mcp.server import get_model

    with pytest.raises(Exception):
        get_model("sqlmesh_example.does_not_exist")


def test_lineage_traces_column_to_upstream_model():
    from sqlmesh_mcp.server import lineage

    deps = lineage("sqlmesh_example.full_model", "num_orders")
    # num_orders = COUNT(DISTINCT id) from incremental_model -- id should show up
    # somewhere in the dependency map's values.
    all_upstream_cols = {col for cols in deps.values() for col in cols}
    assert any("id" in col for col in all_upstream_cols) or any(
        "incremental_model" in key for key in deps
    )


def test_run_test_passes_on_the_untouched_demo_project():
    from sqlmesh_mcp.server import run_test

    result = run_test()
    assert result["success"] is True
    assert result["tests_run"] >= 1
    assert result["failures"] == []
    assert result["errors"] == []


def test_plan_previews_the_initial_environment_without_applying():
    from sqlmesh_mcp.server import _PLAN_CACHE, plan

    result = plan(environment="dev")
    assert result["plan_id"]
    assert result["environment"] == "dev"
    # A brand new project planned against a fresh env should show every model as new,
    # normalized to the same plain 'schema.model' form list_models/get_model use.
    assert set(result["added_models"]) >= {
        "sqlmesh_example.full_model",
        "sqlmesh_example.incremental_model",
        "sqlmesh_example.seed_model",
    }
    # Nothing should have been applied -- the plan is only cached, not run.
    assert result["plan_id"] in _PLAN_CACHE


def test_apply_plan_refuses_without_confirm():
    from sqlmesh_mcp.server import apply_plan, plan

    p = plan(environment="dev_confirm_check")
    with pytest.raises(ValueError, match="confirm=true"):
        apply_plan(p["plan_id"], confirm=False)


def test_apply_plan_rejects_unknown_plan_id():
    from sqlmesh_mcp.server import apply_plan

    with pytest.raises(ValueError, match="No cached plan"):
        apply_plan("not-a-real-plan-id", confirm=True)


def test_apply_plan_actually_applies_when_confirmed():
    from sqlmesh_mcp.server import _PLAN_CACHE, apply_plan, plan

    p = plan(environment="dev_apply_check")
    result = apply_plan(p["plan_id"], confirm=True)
    assert result["applied"] is True
    assert p["plan_id"] not in _PLAN_CACHE
