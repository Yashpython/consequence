"""Tests for the MCP layer: a thin translator over consequence.tools.

The schema-matching tests are pure (no DB). The end-to-end tests are
@pytest.mark.live because call_tool() talks to the real database; they
reset the environment first so both the direct and the MCP-layer call see
identical seed state, which is what makes their results comparable.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import types

from consequence.environment import reset
from consequence.mcp_server import handle_call_tool, handle_list_tools
from consequence.tools import TOOLS, call_tool


def _list_tools() -> list[types.Tool]:
    result = anyio.run(handle_list_tools, None, None)
    return result.tools


def test_lists_exactly_the_expected_tool_names():
    names = {t.name for t in _list_tools()}
    assert names == {t.name for t in TOOLS}


def test_input_schema_matches_schema_py_for_every_tool():
    by_name = {t.name: t for t in _list_tools()}
    assert set(by_name) == {t.name for t in TOOLS}

    for tool_def in TOOLS:
        mcp_tool = by_name[tool_def.name]
        assert mcp_tool.description == tool_def.description
        assert mcp_tool.input_schema == tool_def.json_schema()


@pytest.mark.live
def test_read_tool_end_to_end_matches_impl_directly():
    reset()
    direct = call_tool("get_invoice", {"invoice_id": 3})

    reset()
    call_result = anyio.run(
        handle_call_tool,
        None,
        types.CallToolRequestParams(name="get_invoice", arguments={"invoice_id": 3}),
    )

    assert call_result.isError is False
    assert call_result.structuredContent == direct
    [content] = call_result.content
    assert isinstance(content, types.TextContent)
    assert json.loads(content.text) == direct


@pytest.mark.live
def test_write_tool_end_to_end_matches_impl_directly():
    args = {"invoice_id": 3, "reason": "needs manual check"}

    reset()
    direct = call_tool("flag_for_review", args)

    reset()  # identical seed state again -> the MCP call computes the same new id
    call_result = anyio.run(
        handle_call_tool,
        None,
        types.CallToolRequestParams(name="flag_for_review", arguments=args),
    )

    assert call_result.isError is False
    assert call_result.structuredContent == direct
    [content] = call_result.content
    assert isinstance(content, types.TextContent)
    assert json.loads(content.text) == direct
