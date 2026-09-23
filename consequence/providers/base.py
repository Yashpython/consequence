"""The provider adapter interface.

Same philosophy as the docbench adapter interface: adding a model is a
config change (write a new Provider implementation, point the harness at
it), not a rewrite of the harness loop. The harness only ever depends on
this interface -- never on a specific vendor SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from consequence.tools.schema import ToolDef


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model asked for in a single turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """What a provider returns for one model turn.

    raw_response is stored verbatim (by the harness, into the results DB)
    for later re-analysis -- it is never interpreted by the harness itself.
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_response: Any = None


class Provider(Protocol):
    """Anything the harness can drive one turn at a time.

    `messages` is the provider-neutral conversation so far (a list of plain
    dicts: {"role": ..., "content": ...}, assistant turns additionally
    carrying "tool_calls", tool turns carrying "tool_call_id"/"name"). A
    real adapter translates this into its provider's wire format and
    translates the reply back into a TurnResult; it never lets any
    provider-specific type leak back into the harness.
    """

    model_id: str

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult: ...
