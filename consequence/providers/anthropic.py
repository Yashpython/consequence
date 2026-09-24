"""Anthropic adapter: Messages API via the official `anthropic` SDK.

The SDK itself is imported lazily (inside run_turn/the client property), so
this module -- and its tool/message translation, which is what the offline
tests exercise -- imports fine without the `anthropic` package installed.
Only actually calling run_turn() requires it (`pip install anthropic`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.retry import call_with_backoff
from consequence.tools.schema import ToolDef

DEFAULT_MAX_TOKENS = 4096


def translate_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    """Provider-neutral ToolDef -> the Messages API tool shape."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in tools
    ]


def _stringify(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content)


def translate_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Provider-neutral messages -> (system prompt, Messages API `messages`).

    The first system message becomes the top-level `system` param.
    Assistant tool_calls become tool_use content blocks; a tool result
    becomes a tool_result block on a following user turn.
    """
    system: str | None = None
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            if system is None:
                system = msg["content"]
            continue
        if role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            content: list[dict[str, Any]] = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for call in msg.get("tool_calls") or []:
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                )
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": _stringify(msg["content"]),
                        }
                    ],
                }
            )
        else:
            raise ValueError(f"unknown message role: {role!r}")
    return system, out


def _is_retryable(exc: Exception) -> bool:
    import anthropic

    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def parse_response(response: Any, latency_ms: int, retry_count: int) -> TurnResult:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

    return TurnResult(
        text="".join(text_parts) or None,
        tool_calls=tuple(tool_calls),
        stop_reason=response.stop_reason or "end_turn",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
        raw_response=response.to_dict(),
        retry_count=retry_count,
    )


@dataclass
class AnthropicProvider:
    model_id: str
    api_model: str
    api_key: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = 5
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        system, anthropic_messages = translate_messages(messages)
        anthropic_tools = translate_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "max_tokens": self.max_tokens,
            "messages": anthropic_messages,
            "tools": anthropic_tools,
        }
        if system is not None:
            kwargs["system"] = system

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.messages.create(**kwargs)
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        return parse_response(response, elapsed_ms, retry_count)
