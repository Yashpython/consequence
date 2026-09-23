"""OpenAI Chat Completions adapter (POST /v1/chat/completions, function tools).

The OpenRouter adapter reuses this translation: OpenRouter speaks the same
wire format.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from consequence.providers.base import ToolCall
from consequence.providers.common import (
    HTTPProvider,
    ParsedTurn,
    ProviderResponseError,
    Usage,
    tool_result_text,
)
from consequence.tools.schema import ToolDef

API_URL = "https://api.openai.com/v1/chat/completions"

# A tool call whose arguments aren't valid JSON is passed to the tool layer
# under this key: validation rejects it as an unknown argument, so the model
# gets an ordinary ok=False tool result, and the raw string is kept.
UNPARSEABLE_ARGUMENTS_KEY = "__unparseable_arguments__"


def parse_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {UNPARSEABLE_ARGUMENTS_KEY: raw}
    return parsed if isinstance(parsed, dict) else {UNPARSEABLE_ARGUMENTS_KEY: raw}


class ChatCompletionsProvider(HTTPProvider):
    """Translation shared by every Chat Completions-compatible API."""

    max_tokens_field = "max_completion_tokens"
    # Extra assistant-message fields to replay verbatim, if the API returned them.
    replay_fields: tuple[str, ...] = ()

    @staticmethod
    def tool_to_native(tool: ToolDef) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.json_schema(),
            },
        }

    def translate_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role in ("system", "user"):
                out.append({"role": role, "content": msg["content"]})
            elif role == "assistant":
                native = self.native_for(msg)
                if native is not None:
                    out.append(native)
                    continue
                entry: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
                if msg.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                        for c in msg["tool_calls"]
                    ]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": msg["tool_call_id"],
                            "content": tool_result_text(msg["content"])})
            else:
                raise ValueError(f"unexpected role {role!r}")
        return out

    def build_request(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.spec.api_model,
            "messages": self.translate_messages(messages),
            self.max_tokens_field: self.spec.max_output_tokens,
        }
        if tools:
            body["tools"] = [self.tool_to_native(t) for t in tools]
        body.update(self.spec.params)
        return body

    def parse_response(self, payload: Mapping[str, Any]) -> ParsedTurn:
        if payload.get("error"):
            raise ProviderResponseError(f"error in 2xx response: {payload['error']}")
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderResponseError("response has no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        calls = tuple(
            ToolCall(id=c["id"], name=c["function"]["name"],
                     arguments=parse_arguments(c["function"].get("arguments")))
            for c in raw_calls
        )
        # Replay what we got, restricted to fields valid on an input message.
        native: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if raw_calls:
            native["tool_calls"] = raw_calls
        for key in self.replay_fields:
            if message.get(key) is not None:
                native[key] = message[key]

        usage = payload.get("usage") or {}
        prompt = usage.get("prompt_tokens") or 0
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return ParsedTurn(
            text=message.get("content") or None,
            tool_calls=calls,
            stop_reason=choice.get("finish_reason") or "unknown",
            usage=Usage(
                input_tokens=prompt - cached,
                # completion_tokens already includes reasoning tokens.
                output_tokens=usage.get("completion_tokens") or 0,
                cached_input_tokens=cached,
            ),
            native_assistant=native,
        )


class OpenAIProvider(ChatCompletionsProvider):
    provider_name = "openai"

    def url(self) -> str:
        return API_URL

    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._api_key}", "content-type": "application/json"}
