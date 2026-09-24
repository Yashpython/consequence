"""OpenRouter adapter, for the open-weights model in the lineup.

OpenRouter's chat completions endpoint is wire-compatible with OpenAI's
Chat Completions API (that's the point of it -- one endpoint in front of
many providers), so this reuses the OpenAI adapter's tool/message
translation and response parsing verbatim and only swaps the client
construction (base_url + OPENROUTER_API_KEY) and the retryable-error check
(still the `openai` SDK's exception types, since that's the client used to
reach OpenRouter's OpenAI-compatible endpoint).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from consequence.providers.base import TurnResult
from consequence.providers.openai import parse_response, translate_messages, translate_tools
from consequence.providers.retry import call_with_backoff
from consequence.tools.schema import ToolDef

DEFAULT_MAX_TOKENS = 4096
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

__all__ = ["OpenRouterProvider", "translate_messages", "translate_tools"]


def _is_retryable(exc: Exception) -> bool:
    import openai

    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


@dataclass
class OpenRouterProvider:
    model_id: str
    api_model: str
    api_key: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = 5
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI(api_key=self.api_key, base_url=OPENROUTER_BASE_URL)
        return self._client

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        or_messages = translate_messages(messages)
        or_tools = translate_tools(tools)

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.chat.completions.create(
                model=self.api_model,
                max_completion_tokens=self.max_tokens,
                messages=or_messages,
                tools=or_tools,
            )
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        return parse_response(response, elapsed_ms, retry_count)
