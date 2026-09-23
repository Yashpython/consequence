"""A stdio MCP server exposing the typed tool layer.

This module is deliberately thin: it translates MCP tools/list and
tools/call requests into consequence.tools.schema / consequence.tools.impl
calls and translates the results back. Zero business logic lives here --
validation, business rules, and the audit trail all live in
consequence.tools.impl, which this module never bypasses.

Run standalone with:

    python -m consequence.mcp_server

See docs/mcp.md for why MCP is used here and how to inspect the server
manually.
"""

from __future__ import annotations

import json

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from consequence.tools import TOOLS, call_tool
from consequence.tools.schema import ToolDef


def _to_mcp_tool(tool_def: ToolDef) -> types.Tool:
    return types.Tool(
        name=tool_def.name,
        description=tool_def.description,
        inputSchema=tool_def.json_schema(),
    )


async def handle_list_tools(
    ctx: ServerRequestContext | None,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    del ctx, params  # unused: the tool set is fixed and unpaginated
    return types.ListToolsResult(tools=[_to_mcp_tool(t) for t in TOOLS])


async def handle_call_tool(
    ctx: ServerRequestContext | None,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    del ctx  # unused: no session state is needed to dispatch a tool call
    result = call_tool(params.name, params.arguments or {})
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result))],
        structuredContent=result,
        isError=not result["ok"],
    )


server = Server(
    "consequence",
    version="0.0.1",
    instructions="Tools for the consequence supplier-invoice back office.",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
