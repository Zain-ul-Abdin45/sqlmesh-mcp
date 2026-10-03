"""Integration tests against the real demo SQLMesh project in examples/demo_project.

These call the underlying tool functions directly (not through the MCP
protocol layer -- see test_protocol.py for that) to keep them fast and
focused on our own logic.

See TEST_CASES.md for a plain-English index of every case covered here and
in test_protocol.py.
"""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

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


# ---------------------------------------------------------------------------
# Read-only tools that don't require a prior plan/apply
# ---------------------------------------------------------------------------


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


def test_get_model_raises_tool_error_with_the_real_message_for_unknown_model():
    from sqlmesh_mcp.server import get_model

    with pytest.raises(ToolError, match="does_not_exist"):
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


def test_list_environments_is_empty_before_anything_is_applied():
    from sqlmesh_mcp.server import list_environments

    assert list_environments() == []


def test_profile_model_requires_an_applied_model():
    from sqlmesh_mcp.server import profile_model

    with pytest.raises(ToolError):
        profile_model("sqlmesh_example.incremental_model")


# ---------------------------------------------------------------------------
# plan() / apply_plan() -- the two-step preview/confirm flow
# ---------------------------------------------------------------------------


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
    with pytest.raises(ToolError, match="confirm=true"):
        apply_plan(p["plan_id"], confirm=False)


def test_apply_plan_rejects_unknown_plan_id():
    from sqlmesh_mcp.server import apply_plan

    with pytest.raises(ToolError, match="No cached plan"):
        apply_plan("not-a-real-plan-id", confirm=True)


def test_apply_plan_actually_applies_when_confirmed():
    from sqlmesh_mcp.server import _PLAN_CACHE, apply_plan, plan

    p = plan(environment="dev_apply_check")
    result = apply_plan(p["plan_id"], confirm=True)
    assert result["applied"] is True
    assert p["plan_id"] not in _PLAN_CACHE


# ---------------------------------------------------------------------------
# Tools that only make sense once a plan has actually been applied --
# run_audit and diff_environment were previously completely untested; calling
# either against an unversioned project raises a SQLMeshError (confirmed
# manually: "Cannot audit ... it has not been versioned yet. Apply a plan
# first."), so these fixtures apply a real plan before exercising them.
# ---------------------------------------------------------------------------


@pytest.fixture
def applied_prod_env():
    """Plans and applies against 'prod' so audits/diffs/runs have real, versioned data."""
    from sqlmesh_mcp.server import apply_plan, plan

    p = plan(environment="prod")
    apply_plan(p["plan_id"], confirm=True)
    return "prod"


def test_run_audit_passes_once_the_project_has_been_applied(applied_prod_env):
    from sqlmesh_mcp.server import run_audit

    result = run_audit()
    assert result["passed"] is True


def test_diff_environment_shows_no_diff_immediately_after_apply(applied_prod_env):
    from sqlmesh_mcp.server import diff_environment

    result = diff_environment(applied_prod_env)
    assert result == {"environment": "prod", "has_diff": False}


def test_list_environments_shows_the_applied_environment(applied_prod_env):
    from sqlmesh_mcp.server import list_environments

    envs = list_environments()
    names = {e["name"] for e in envs}
    assert "prod" in names


def test_run_reports_nothing_to_do_immediately_after_apply(applied_prod_env):
    """apply_plan already backfilled every interval, so a run right after has nothing due."""
    from sqlmesh_mcp.server import run

    result = run(environment=applied_prod_env, confirm=True)
    assert result["status"] == "NOTHING_TO_DO"


def test_run_refuses_without_confirm(applied_prod_env):
    from sqlmesh_mcp.server import run

    with pytest.raises(ToolError, match="confirm=true"):
        run(environment=applied_prod_env, confirm=False)


# ---------------------------------------------------------------------------
# profile_model / propose_cleansing_plan / apply_cleansing_plan -- dataveil's
# aggregate-only profiling + plan-based cleansing, wired in as new tools.
# incremental_model.sql has a synthetic 'customer_email' column added
# specifically for this demo (see the model's comment).
# ---------------------------------------------------------------------------


def test_profile_model_never_contains_a_literal_email(applied_prod_env):
    from sqlmesh_mcp.server import profile_model

    profile = profile_model("sqlmesh_example.incremental_model")
    assert profile["row_count"] > 0
    columns = {c["name"]: c for c in profile["columns"]}
    assert "customer_email" in columns
    assert columns["customer_email"]["format_signatures"]

    serialized = json.dumps(profile)
    assert "user1@example.com" not in serialized
    assert "@example.com" not in serialized  # not even the constant suffix


def test_propose_cleansing_plan_tags_the_synthetic_email_column(applied_prod_env):
    from sqlmesh_mcp.server import propose_cleansing_plan

    proposal = propose_cleansing_plan("sqlmesh_example.incremental_model")
    assert proposal["model"] == "sqlmesh_example.incremental_model"
    tags = {c["column"]: c["tag"] for c in proposal["classifications"]}
    assert tags["customer_email"] == "PII:EMAIL"
    assert tags["event_date"] == "none"  # a DATE column, not a false-positive phone match
    assert "mask" in proposal["operation_vocabulary"]
    assert "drop_column" in proposal["operation_vocabulary"]


def test_profile_classify_and_apply_are_all_audit_logged(applied_prod_env, tmp_path, monkeypatch):
    """Runs before the masking tests below -- it needs customer_email still
    unmasked to see PII:EMAIL classified, and it masks it itself here."""
    from dataveil.audit import AuditLog

    from sqlmesh_mcp.server import apply_cleansing_plan, profile_model, propose_cleansing_plan

    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DATAVEIL_AUDIT_LOG_PATH", str(log_path))

    profile_model("sqlmesh_example.incremental_model")
    proposal = propose_cleansing_plan("sqlmesh_example.incremental_model")
    tags = {c["column"]: c["tag"] for c in proposal["classifications"]}
    assert tags["customer_email"] == "PII:EMAIL"
    mask_plan = [
        {
            "operation": "mask",
            "column": "customer_email",
            "params": {"method": "hash"},
            "rationale": "x",
        }
    ]
    apply_cleansing_plan("sqlmesh_example.incremental_model", mask_plan, confirm=True)

    events = AuditLog(log_path).read_all()
    kinds = [e.kind for e in events]
    assert kinds == ["profile", "classify", "execute_plan"]
    assert all(e.table == "sqlmesh_example.incremental_model" for e in events)
    # never a literal value -- just counts/tags/operation references
    serialized = json.dumps([e.to_dict() for e in events])
    assert "user1@example.com" not in serialized


def test_apply_cleansing_plan_refuses_without_confirm(applied_prod_env):
    from sqlmesh_mcp.server import apply_cleansing_plan

    with pytest.raises(ToolError, match="confirm=true"):
        apply_cleansing_plan("sqlmesh_example.incremental_model", [], confirm=False)


def test_apply_cleansing_plan_rejects_unknown_operation_before_touching_data(applied_prod_env):
    from sqlmesh_mcp.server import apply_cleansing_plan

    bad_plan = [
        {
            "operation": "drop_table",  # not in the vocabulary
            "column": "customer_email",
            "params": {},
            "rationale": "x",
        }
    ]
    with pytest.raises(ToolError, match="unknown operation"):
        apply_cleansing_plan("sqlmesh_example.incremental_model", bad_plan, confirm=True)


def test_apply_cleansing_plan_masks_the_synthetic_email_column(applied_prod_env):
    from sqlmesh_mcp.context import get_context
    from sqlmesh_mcp.server import apply_cleansing_plan

    plan = [
        {
            "operation": "mask",
            "column": "customer_email",
            "params": {"method": "hash"},
            "rationale": "PII:EMAIL detected at match_rate=1.0",
        }
    ]
    result = apply_cleansing_plan("sqlmesh_example.incremental_model", plan, confirm=True)
    assert result["applied_steps"][0]["operation"] == "mask"

    ctx = get_context()
    df = ctx.fetchdf("SELECT customer_email FROM sqlmesh_example.incremental_model LIMIT 1")
    value = df["customer_email"].iloc[0]
    assert value != "user1@example.com"
    assert len(value) == 32  # md5 hex digest


# ---------------------------------------------------------------------------
# register_cleansing_plan / approve_cleansing_plan -- the optional
# approval-gated path in front of apply_cleansing_plan, enabled by
# DATAVEIL_REQUIRE_PLAN_APPROVAL. Off by default (the tests above never set
# it), so apply_cleansing_plan keeps accepting a plan directly unless a
# server opts into this.
# ---------------------------------------------------------------------------

MASK_PLAN = [
    {
        "operation": "mask",
        "column": "customer_email",
        "params": {"method": "hash"},
        "rationale": "PII:EMAIL",
    }
]


def test_register_cleansing_plan_validates_the_plan_immediately(applied_prod_env):
    from sqlmesh_mcp.server import register_cleansing_plan

    bad_plan = [
        {"operation": "drop_table", "column": "customer_email", "params": {}, "rationale": "x"}
    ]
    with pytest.raises(ToolError, match="unknown operation"):
        register_cleansing_plan("sqlmesh_example.incremental_model", bad_plan)


def test_register_cleansing_plan_returns_an_unapproved_pending_plan(applied_prod_env):
    from sqlmesh_mcp.server import register_cleansing_plan

    result = register_cleansing_plan("sqlmesh_example.incremental_model", MASK_PLAN)
    assert result["plan_id"]
    assert result["model"] == "sqlmesh_example.incremental_model"
    assert result["approved"] is False


def test_approve_cleansing_plan_unknown_id_raises(applied_prod_env):
    from sqlmesh_mcp.server import approve_cleansing_plan

    with pytest.raises(ToolError, match="no pending plan"):
        approve_cleansing_plan("not-a-real-id", approved_by="alice@example.com")


def test_apply_cleansing_plan_requires_plan_id_when_approval_is_required(
    applied_prod_env, monkeypatch
):
    from sqlmesh_mcp.server import apply_cleansing_plan

    monkeypatch.setenv("DATAVEIL_REQUIRE_PLAN_APPROVAL", "true")
    with pytest.raises(ToolError, match="DATAVEIL_REQUIRE_PLAN_APPROVAL"):
        apply_cleansing_plan("sqlmesh_example.incremental_model", plan=MASK_PLAN, confirm=True)


def test_apply_cleansing_plan_rejects_an_unapproved_plan_id(applied_prod_env, monkeypatch):
    from sqlmesh_mcp.server import apply_cleansing_plan, register_cleansing_plan

    pending = register_cleansing_plan("sqlmesh_example.incremental_model", MASK_PLAN)
    monkeypatch.setenv("DATAVEIL_REQUIRE_PLAN_APPROVAL", "true")
    with pytest.raises(ToolError, match="has not been approved"):
        apply_cleansing_plan(
            "sqlmesh_example.incremental_model", plan_id=pending["plan_id"], confirm=True
        )


def test_apply_cleansing_plan_rejects_a_plan_id_registered_for_a_different_model(
    applied_prod_env, monkeypatch
):
    from sqlmesh_mcp.server import (
        apply_cleansing_plan,
        approve_cleansing_plan,
        register_cleansing_plan,
    )

    pending = register_cleansing_plan("sqlmesh_example.incremental_model", MASK_PLAN)
    approve_cleansing_plan(pending["plan_id"], approved_by="alice@example.com")
    monkeypatch.setenv("DATAVEIL_REQUIRE_PLAN_APPROVAL", "true")
    with pytest.raises(ToolError, match="was registered for"):
        apply_cleansing_plan(
            "sqlmesh_example.full_model", plan_id=pending["plan_id"], confirm=True
        )


def test_register_approve_and_apply_cleansing_plan_end_to_end(applied_prod_env, monkeypatch):
    from sqlmesh_mcp.context import get_context
    from sqlmesh_mcp.server import (
        _PLAN_REGISTRY,
        apply_cleansing_plan,
        approve_cleansing_plan,
        register_cleansing_plan,
    )

    pending = register_cleansing_plan("sqlmesh_example.incremental_model", MASK_PLAN)
    approved = approve_cleansing_plan(pending["plan_id"], approved_by="alice@example.com")
    assert approved["approved"] is True
    assert approved["approved_by"] == "alice@example.com"

    monkeypatch.setenv("DATAVEIL_REQUIRE_PLAN_APPROVAL", "true")
    result = apply_cleansing_plan(
        "sqlmesh_example.incremental_model", plan_id=pending["plan_id"], confirm=True
    )
    assert result["applied_steps"][0]["operation"] == "mask"

    ctx = get_context()
    df = ctx.fetchdf("SELECT customer_email FROM sqlmesh_example.incremental_model LIMIT 1")
    assert len(df["customer_email"].iloc[0]) == 32  # md5 hex digest, masked again

    # the plan is discarded after a successful apply -- can't be replayed
    assert _PLAN_REGISTRY.get(pending["plan_id"]) is None
    with pytest.raises(ToolError, match="no registered plan"):
        apply_cleansing_plan(
            "sqlmesh_example.incremental_model", plan_id=pending["plan_id"], confirm=True
        )
