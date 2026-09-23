"""OpenRouter adapter, for the open-weights model.

OpenRouter speaks the Chat Completions wire format, so translation is
inherited from the OpenAI adapter. What differs is routing: a model slug on
OpenRouter can be served by many upstream hosts, each with its own
quantization and price. The registry entry pins a `routing` object with
allow_fallbacks: false, so every turn is served by the pinned host -- which
is what makes both the behaviour and the pinned price hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from consequence.providers.openai import ChatCompletionsProvider
from consequence.tools.schema import ToolDef

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(ChatCompletionsProvider):
    provider_name = "openrouter"
    max_tokens_field = "max_tokens"
    # OpenRouter returns reasoning as message.reasoning_details and asks for
    # it back unchanged to keep reasoning continuous across tool calls.
    replay_fields = ("reasoning_details",)

    def url(self) -> str:
        return API_URL

    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._api_key}", "content-type": "application/json"}

    def build_request(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> dict[str, Any]:
        body = super().build_request(messages, tools)
        body["provider"] = dict(self.spec.routing or {})
        # Ask OpenRouter to report its own billed cost in the raw response:
        # a cross-check on the pinned prices, never a substitute for them.
        body["usage"] = {"include": True}
        return body
