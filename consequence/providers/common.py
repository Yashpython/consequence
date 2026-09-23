"""Machinery shared by the HTTP provider adapters.

Four things live here, each written once so the adapters can't drift apart:

  - exact cost: token counts from the provider's own usage block, priced at
    the rates pinned in models.yaml. Nothing is estimated -- if a turn bills
    tokens in a category that has no pinned price, its cost is unknown
    (None), never guessed.
  - the retry policy: exponential backoff on HTTP 429 and 5xx only. A 2xx
    response is a completed turn and is never retried; a transport error
    (timeout, dropped connection) is never retried either, because we can't
    know whether the provider completed -- and billed -- the turn.
  - the per-episode ledger: every TurnResult an adapter returns, in order,
    so the raw responses, retry count and cost can be written to the results
    DB after the harness finishes (see consequence/runner.py).
  - native replay: the harness's provider-neutral history only carries text
    and tool calls, but some APIs require provider-specific state to be sent
    back verbatim on the next request (Anthropic thinking-block signatures,
    Gemini thought signatures, OpenRouter reasoning details). Each adapter
    remembers the exact assistant content it received, keyed by the turn's
    tool-call ids, and replays that instead of a lossy reconstruction.

Adapters talk to each API over plain HTTP (httpx) rather than vendor SDKs:
it keeps the retry policy the only retry policy (SDKs retry on their own,
including on timeouts), and it lets the raw response body be stored as the
exact bytes the provider sent.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from consequence.providers.base import ToolCall, TurnResult
from consequence.tools.schema import ToolDef

if TYPE_CHECKING:
    from consequence.providers.registry import ModelSpec

_PER_MTOK = Decimal(1_000_000)
DEFAULT_TIMEOUT_S = 600.0


# -- pricing and cost ------------------------------------------------------


@dataclass(frozen=True)
class PriceTier:
    """USD per million tokens. None means "no pinned price for this category"."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None


@dataclass(frozen=True)
class Pricing:
    base: PriceTier
    # Some providers bill a whole request at a higher tier once its prompt
    # exceeds a threshold (Gemini Pro above 200k, OpenAI above 272k). The
    # threshold is per request, i.e. per turn, not per episode.
    long_context: PriceTier | None = None
    long_context_threshold: int | None = None

    def tier_for(self, prompt_tokens: int) -> PriceTier:
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and prompt_tokens > self.long_context_threshold
        ):
            return self.long_context
        return self.base


@dataclass(frozen=True)
class Usage:
    """One turn's billed tokens, split by billing category.

    The categories are disjoint: input_tokens is prompt tokens billed at the
    full input rate, excluding cached reads and cache writes.
    output_tokens includes any reasoning/thinking tokens, which every
    provider here bills as output.
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cached_input_tokens + self.cache_write_tokens


class MissingPriceError(ValueError):
    """Tokens were billed in a category models.yaml has no pinned price for."""


def compute_cost(pricing: Pricing, usage: Usage) -> float:
    """Exact USD cost of one turn: actual token counts x pinned prices."""
    tier = pricing.tier_for(usage.prompt_tokens)
    total = Decimal(usage.input_tokens) * tier.input_per_mtok
    total += Decimal(usage.output_tokens) * tier.output_per_mtok
    for tokens, price, label in (
        (usage.cached_input_tokens, tier.cached_input_per_mtok, "cached input"),
        (usage.cache_write_tokens, tier.cache_write_per_mtok, "cache write"),
    ):
        if tokens:
            if price is None:
                raise MissingPriceError(f"{tokens} {label} token(s) billed but no price is pinned")
            total += Decimal(tokens) * price
    return float(total / _PER_MTOK)


# -- retry -----------------------------------------------------------------


def is_retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 6
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = random.random

    def delay(self, attempt: int, retry_after: str | None) -> float:
        backoff = min(self.max_delay_s, self.base_delay_s * 2**attempt)
        backoff *= 0.5 + 0.5 * self.jitter()  # "equal jitter": [backoff/2, backoff]
        if retry_after is not None:
            try:
                backoff = max(backoff, min(self.max_delay_s, float(retry_after)))
            except ValueError:
                pass  # an HTTP-date Retry-After; the exponential delay stands
        return backoff


class ProviderHTTPError(RuntimeError):
    """A non-2xx response that was not (or no longer) retried."""

    def __init__(self, status_code: int, body: str, retries: int):
        self.status_code = status_code
        self.body = body
        self.retries = retries
        super().__init__(f"HTTP {status_code} after {retries} retr(y/ies): {body[:500]}")


class ProviderResponseError(RuntimeError):
    """A 2xx response that doesn't contain a usable model turn. Never retried."""


@dataclass(frozen=True)
class HTTPAttempt:
    response: httpx.Response
    retries: int
    latency_ms: int  # of the final, successful attempt only


def post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    policy: RetryPolicy,
) -> HTTPAttempt:
    """POST once, retrying only on 429/5xx.

    Transport exceptions propagate immediately: a read timeout may mean the
    provider finished (and billed) the turn, and retrying would silently
    run the turn twice.
    """
    content = json.dumps(body).encode()
    retries = 0
    while True:
        started = time.monotonic()
        response = client.post(url, content=content, headers=dict(headers))
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code < 300:
            return HTTPAttempt(response=response, retries=retries, latency_ms=latency_ms)
        if not is_retryable(response.status_code) or retries >= policy.max_retries:
            raise ProviderHTTPError(response.status_code, response.text, retries)
        policy.sleep(policy.delay(retries, response.headers.get("retry-after")))
        retries += 1


# -- the episode ledger ----------------------------------------------------


@dataclass(frozen=True)
class EpisodeUsage:
    """What one episode cost and how it got there, for the results DB.

    turns holds one TurnResult per assistant transcript row the harness
    wrote, in the same order.
    """

    model_id: str
    turns: tuple[TurnResult, ...]
    retry_count: int

    @property
    def cost_usd(self) -> float | None:
        costs = [t.cost_usd for t in self.turns]
        if any(c is None for c in costs):
            return None  # unknown, not estimated
        return float(sum(Decimal(str(c)) for c in costs))

    @property
    def raw_responses(self) -> list[str | None]:
        return [t.raw_response for t in self.turns]


def tool_result_text(content: Any) -> str:
    """The exact string a tool result is sent to the model as."""
    return content if isinstance(content, str) else json.dumps(content, default=str)


# -- the base adapter ------------------------------------------------------


@dataclass(frozen=True)
class ParsedTurn:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    usage: Usage
    native_assistant: Any  # replayed verbatim on later requests


class HTTPProvider:
    """Base class: request/response translation is the only per-provider part.

    Subclasses implement tool_to_native(), build_request() and
    parse_response(), plus url()/headers().
    """

    provider_name: str = ""

    def __init__(
        self,
        spec: ModelSpec,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
    ):
        if spec.provider != self.provider_name:
            raise ValueError(f"{spec.id} is a {spec.provider} model, not {self.provider_name}")
        self.spec = spec
        # The exact pinned API string -- the harness writes this onto every
        # episode row as model_id.
        self.model_id = spec.api_model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
        self._retry = retry or RetryPolicy()
        self.start_episode()

    # -- episode lifecycle --

    def start_episode(self) -> None:
        self._turns: list[TurnResult] = []
        self._retries = 0
        self._native: dict[tuple[str, ...], Any] = {}
        self._call_counter = 0

    def finish_episode(self) -> EpisodeUsage:
        usage = EpisodeUsage(
            model_id=self.model_id, turns=tuple(self._turns), retry_count=self._retries
        )
        self.start_episode()
        return usage

    # -- the Provider protocol --

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        body = self.build_request(messages, tools)
        try:
            attempt = post_with_retry(
                self._client, self.url(), body=body, headers=self.headers(), policy=self._retry
            )
        except ProviderHTTPError as exc:
            self._retries += exc.retries
            raise
        self._retries += attempt.retries

        raw = attempt.response.text
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(f"non-JSON 2xx response: {raw[:500]}") from exc
        parsed = self.parse_response(payload)

        try:
            cost: float | None = compute_cost(self.spec.pricing, parsed.usage)
        except MissingPriceError:
            cost = None

        if parsed.tool_calls:
            self._native[tuple(c.id for c in parsed.tool_calls)] = parsed.native_assistant

        turn = TurnResult(
            text=parsed.text,
            tool_calls=parsed.tool_calls,
            stop_reason=parsed.stop_reason,
            input_tokens=parsed.usage.prompt_tokens,
            output_tokens=parsed.usage.output_tokens,
            latency_ms=attempt.latency_ms,
            raw_response=raw,
            retries=attempt.retries,
            cost_usd=cost,
        )
        self._turns.append(turn)
        return turn

    # -- helpers for subclasses --

    def native_for(self, message: Mapping[str, Any]) -> Any | None:
        """The verbatim provider content for a neutral assistant message, if any."""
        calls = message.get("tool_calls") or []
        if not calls:
            return None
        return self._native.get(tuple(c["id"] for c in calls))

    def new_call_id(self) -> str:
        """For providers that don't assign tool-call ids themselves."""
        self._call_counter += 1
        return f"local-call-{self._call_counter}"

    def served_model(self, payload: Mapping[str, Any]) -> str | None:
        """The model version the provider says actually served a response."""
        return payload.get("model")

    # -- per-provider --

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    @staticmethod
    def tool_to_native(tool: ToolDef) -> dict[str, Any]:
        raise NotImplementedError

    def build_request(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def parse_response(self, payload: Mapping[str, Any]) -> ParsedTurn:
        raise NotImplementedError


def split_system(messages: Sequence[Mapping[str, Any]]) -> tuple[str | None, list[Mapping[str, Any]]]:
    """Pull system messages out of a neutral conversation (for APIs that take
    the system prompt as a separate field)."""
    system = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    return ("\n\n".join(system) if system else None), rest


__all__ = [
    "EpisodeUsage",
    "HTTPProvider",
    "MissingPriceError",
    "ParsedTurn",
    "PriceTier",
    "Pricing",
    "ProviderHTTPError",
    "ProviderResponseError",
    "RetryPolicy",
    "Usage",
    "compute_cost",
    "is_retryable",
    "post_with_retry",
    "split_system",
    "tool_result_text",
]
