"""End-to-end tests through the real MCP protocol, not just direct function calls.

These matter separately from test_server.py: calling the tool functions
directly and catching a Python exception proves our own logic is right, but
says nothing about what actually crosses the wire to an agent. This file
caught a real bug during development -- the MCP SDK silently replaces any
exception that isn't a ToolError with a generic "Error executing tool <name>"
(no further detail at all), dropping the actual message -- that no amount of
direct-call testing would have found. With ToolError, the SDK still prefixes
"Error executing tool <name>: ", but the real message now follows it.

Spawns the server as a real subprocess and talks to it over stdio, the same
way a real MCP client (Claude Desktop, etc.) would. Each test opens its own
session inline (rather than via a fixture) because sharing an async
generator fixture's stdio_client/ClientSession context managers across
pytest-asyncio's per-test task boundaries triggers anyio "cancel scope
exited in a different task" errors on teardown -- opening and closing the
session within a single test's task avoids that entirely.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEMO_PROJECT = Path(__file__).parent.parent / "examples" / "demo_project"


@asynccontextmanager
async def open_session():
    env = os.environ.copy()
    env["SQLMESH_PROJECT_PATH"] = str(DEMO_PROJECT)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "sqlmesh_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def test_lists_all_ten_tools():
    async with open_session() as session:
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == {
            "list_models",
            "get_model",
            "plan",
            "apply_plan",
            "lineage",
            "run_audit",
            "run_test",
            "diff_environment",
            "list_environments",
            "run",
        }


async def test_mutating_tools_are_flagged_destructive_and_not_read_only():
    async with open_session() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in ["apply_plan", "run"]:
            assert tools[name].annotations.read_only_hint is False
            assert tools[name].annotations.destructive_hint is True


async def test_read_only_tools_are_flagged_read_only():
    async with open_session() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in [
            "list_models",
            "get_model",
            "plan",
            "lineage",
            "run_audit",
            "run_test",
            "diff_environment",
            "list_environments",
        ]:
            assert tools[name].annotations.read_only_hint is True


async def test_list_models_call_round_trips_real_data():
    async with open_session() as session:
        result = await session.call_tool("list_models", {})
        assert result.is_error is not True
        assert "sqlmesh_example.full_model" in str(result.content)


async def test_apply_plan_without_confirm_is_a_tool_error_with_the_real_message():
    """The regression this file exists to catch: without ToolError, this would
    come back as only the generic "Error executing tool apply_plan" with no
    further detail -- instead of that prefix followed by the actual "Refusing
    to apply without confirm=true..." message the agent needs to see.
    """
    async with open_session() as session:
        result = await session.call_tool("apply_plan", {"plan_id": "fake", "confirm": False})
        assert result.is_error is True
        text = str(result.content)
        assert "confirm=true" in text
        assert "changes real data in the target warehouse" in text


async def test_get_model_for_unknown_model_is_a_tool_error_with_the_real_message():
    async with open_session() as session:
        result = await session.call_tool("get_model", {"model_name": "does.not_exist"})
        assert result.is_error is True
        text = str(result.content)
        assert "does.not_exist" in text or "does_not_exist" in text
        assert "Cannot find model" in text


async def test_session_survives_a_tool_error_and_keeps_working():
    """A tool error must not take down the server process or the session --
    confirmed by making a real, successful call right after two failures.
    """
    async with open_session() as session:
        await session.call_tool("apply_plan", {"plan_id": "fake", "confirm": False})
        await session.call_tool("get_model", {"model_name": "does.not_exist"})

        result = await session.call_tool("list_models", {})
        assert result.is_error is not True
