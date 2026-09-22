"""The typed tool layer: the only write path to the environment database.

The model never sees or emits SQL -- it calls a tool by name with a JSON
object of arguments, gets back {"ok", "data", "error"}, and that's the
entire interface. See schema.py for the tool definitions and impl.py for
the implementations and the business rules they enforce.
"""

from consequence.tools.impl import ToolError, call_tool
from consequence.tools.schema import TOOLS, ToolDef, ToolParameter

__all__ = ["TOOLS", "ToolDef", "ToolError", "ToolParameter", "call_tool"]
