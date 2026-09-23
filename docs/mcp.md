# The tool layer over MCP

`consequence/mcp_server.py` exposes every tool in `consequence/tools/schema.py`
over the Model Context Protocol (stdio transport), using the official
`mcp` Python SDK.

## Why MCP

The benchmark needs one provider-neutral tool surface that any model adapter
can consume, without coupling the tool definitions or their implementations
to a specific vendor's SDK or tool-calling wire format. MCP gives us that:
`consequence/tools/schema.py` describes each tool once (name, description,
JSON Schema for arguments), `consequence/tools/impl.py` implements each tool
once, and `consequence/mcp_server.py` is a thin translation layer with zero
business logic of its own -- it turns `tools/list` and `tools/call` requests
into calls against `consequence.tools.impl` and turns the results back into
MCP content. An adapter for a given model provider talks to this one server;
none of them import `consequence.tools.impl` directly.

## Tool descriptions are part of the experiment

Every `ToolDef.description` and `ToolParameter.description` in
`consequence/tools/schema.py` is prompt text the agent under test reads to
decide whether and how to call a tool. That makes it part of the
experimental apparatus, not incidental documentation.

**Keep tool descriptions byte-identical across models and across runs.**
If a description changes -- even a wording fix -- that is a new
experimental condition. Episodes graded before the change and episodes
graded after it are not comparable, because the agent literally saw
different instructions. If a description must change, treat it like any
other change to the benchmark: note why, and don't compare results across
the change without saying so.

## Running the server standalone

Start it directly:

```bash
python -m consequence.mcp_server
```

This blocks, speaking MCP over stdin/stdout -- it's meant to be launched by
an MCP client, not read directly. To inspect it interactively (list tools,
call one by hand, see the exact JSON schema and results), use the official
MCP Inspector against the same command:

```bash
npx @modelcontextprotocol/inspector python -m consequence.mcp_server
```

Both require `DATABASE_URL` to point at a running environment database (see
the repo root README and `make up`) -- every tool call, read or write, talks
to Postgres.
