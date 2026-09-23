"""Provider adapters: translate the harness's provider-neutral turn protocol
into a specific model API's wire format, and back. See base.py for the
interface every adapter implements and mock.py for the zero-spend test double.
"""

from consequence.providers.base import Provider, ToolCall, TurnResult
from consequence.providers.mock import MockProvider

__all__ = ["MockProvider", "Provider", "ToolCall", "TurnResult"]
