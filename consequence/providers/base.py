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

    raw_response is the provider's response body exactly as received. It is
    never interpreted by the harness; consequence/runner.py stores it
    verbatim in the transcripts table for later re-analysis.

    retries counts HTTP 429/5xx retries it took to get this turn; cost_usd
    is the turn's exact cost from pinned prices, or None when it can't be
    computed exactly (never an estimate).
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_response: Any = None
    retries: int = 0
    cost_usd: float | None = None


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


class RecordingProvider(Provider, Protocol):
    """A Provider that keeps a per-episode ledger of what it did.

    The harness only ever sees the Provider half. consequence/runner.py
    calls start_episode() before handing the provider to the harness and
    finish_episode() after, and writes the returned ledger (raw responses,
    retry count, cost) to the results DB -- so the harness itself never has
    to know about cost or retries.
    """

    def start_episode(self) -> None: ...

    def finish_episode(self) -> Any: ...
