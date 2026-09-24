"""OpenAI adapter: Chat Completions API via the official `openai` SDK.

The SDK is imported lazily (inside the client property), so this module --
and its tool/message translation, exercised offline by the tests -- imports
fine without the `openai` package installed. Only run_turn() needs it.
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
    """Provider-neutral ToolDef -> OpenAI's function-calling tool shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.json_schema(),
            },
        }
        for t in tools
    ]


def _stringify(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content)


def translate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Provider-neutral messages -> Chat Completions `messages`.

    system/user map straight across. An assistant turn's tool_calls become
    the OpenAI tool_calls shape (arguments JSON-encoded, as the API
    requires). A tool result becomes a role="tool" message keyed by
    tool_call_id.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": msg["content"]})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
            calls = msg.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }
                    for call in calls
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": _stringify(msg["content"]),
                }
            )
        else:
            raise ValueError(f"unknown message role: {role!r}")
    return out


def _is_retryable(exc: Exception) -> bool:
    import openai

    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


def parse_response(response: Any, latency_ms: int, retry_count: int) -> TurnResult:
    choice = response.choices[0]
    message = choice.message
    tool_calls = tuple(
        ToolCall(id=c.id, name=c.function.name, arguments=json.loads(c.function.arguments))
        for c in (message.tool_calls or [])
    )
    return TurnResult(
        text=message.content,
        tool_calls=tool_calls,
        stop_reason=choice.finish_reason or "stop",
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        latency_ms=latency_ms,
        raw_response=response.to_dict(),
        retry_count=retry_count,
    )


@dataclass
class OpenAIProvider:
    model_id: str
    api_model: str
    api_key: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = 5
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI(api_key=self.api_key)
        return self._client

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        openai_messages = translate_messages(messages)
        openai_tools = translate_tools(tools)

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.chat.completions.create(
                model=self.api_model,
                max_completion_tokens=self.max_tokens,
                messages=openai_messages,
                tools=openai_tools,
            )
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        return parse_response(response, elapsed_ms, retry_count)
