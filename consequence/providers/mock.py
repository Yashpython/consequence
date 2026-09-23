"""A scripted Provider for tests: zero API spend, zero network calls.

Build a MockProvider with a list of TurnResults (and, optionally,
Exception instances) and it replays them one per run_turn() call,
regardless of what messages/tools it's given. This is enough to simulate:
  - an agent that calls tools correctly and then stops
  - an agent that fabricates completion without calling any tool
    (a TurnResult with no tool_calls as the very first turn)
  - an agent that loops (a script that never runs out of tool-calling turns)
  - an agent whose provider call errors (an Exception in the script)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from consequence.providers.base import TurnResult
from consequence.tools.schema import ToolDef


@dataclass
class MockProvider:
    model_id: str
    script: list[TurnResult | Exception]
    _index: int = field(default=0, init=False, repr=False)

    def run_turn(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> TurnResult:
        del messages, tools  # scripted: the reply doesn't depend on what it's asked
        if self._index >= len(self.script):
            raise RuntimeError(
                f"{self.model_id}: mock script exhausted after {self._index} turn(s)"
            )
        item = self.script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item
