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

import dataclasses
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


def served_model_from_raw(raw: Any) -> str | None:
    """`model@provider`: the model slug OpenRouter served and the upstream
    host it routed the call to. The same open-weights model can be served by
    different hosts (with different quantizations), so the host is part of
    what served the call."""
    if not isinstance(raw, dict) or not raw.get("model"):
        return None
    return f"{raw['model']}@{raw['provider']}" if raw.get("provider") else raw["model"]


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
    # None sends no temperature, so the provider's default applies. Set for
    # judging (consequence.graders.judge), where it is part of the condition.
    temperature: float | None = None
    _client: Any = field(default=None, init=False, repr=False)

    def _resolved_api_key(self) -> str:
        """OPENROUTER_API_KEY, never OPENAI_API_KEY.

        The openai SDK, given api_key=None, reads OPENAI_API_KEY from the
        environment, which would send an OpenAI secret to openrouter.ai (and
        fail auth there). So the key is resolved here and must be present.
        """
        from consequence.config import load_config

        key = self.api_key or load_config().openrouter_api_key
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set; the OpenRouter adapter never "
                               "falls back to OPENAI_API_KEY")
        return key

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI(api_key=self._resolved_api_key(),
                                         base_url=OPENROUTER_BASE_URL)
        return self._client

    def _request(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> dict[str, Any]:
        """The keyword arguments for chat.completions.create(), built without the SDK."""
        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "max_completion_tokens": self.max_tokens,
            "messages": translate_messages(messages),
            "tools": translate_tools(tools),
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        return kwargs

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        kwargs = self._request(messages, tools)

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.chat.completions.create(**kwargs)
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        result = parse_response(response, elapsed_ms, retry_count)
        return dataclasses.replace(result,
                                   served_model=served_model_from_raw(result.raw_response))
