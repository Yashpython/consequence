"""Google adapter: Gemini API via the official `google-genai` SDK.

The SDK is imported lazily (inside the client property), so this module --
and its tool/message translation, exercised offline by the tests -- imports
fine without the `google-genai` package installed. Only run_turn() needs it.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.retry import call_with_backoff
from consequence.tools.schema import ToolDef

DEFAULT_MAX_OUTPUT_TOKENS = 4096


def translate_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    """Provider-neutral ToolDef -> Gemini's function_declarations shape."""
    return [
        {
            "function_declarations": [
                {"name": t.name, "description": t.description, "parameters": t.json_schema()}
                for t in tools
            ]
        }
    ]


def translate_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Provider-neutral messages -> (system_instruction, Gemini `contents`).

    Gemini has only "user" and "model" roles: our "assistant" maps to
    "model", tool calls become function_call parts, and a tool result
    becomes a function_response part on a "user" turn (Gemini's convention
    -- there is no separate tool role).
    """
    system: str | None = None
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            if system is None:
                system = msg["content"]
            continue
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for call in msg.get("tool_calls") or []:
                parts.append({"function_call": {"name": call["name"], "args": call["arguments"]}})
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": msg["name"],
                                "response": _as_response_dict(msg["content"]),
                            }
                        }
                    ],
                }
            )
        else:
            raise ValueError(f"unknown message role: {role!r}")
    return system, contents


def _as_response_dict(content: Any) -> dict[str, Any]:
    """Gemini function_response.response must be a JSON object, not a bare
    string/number/list -- wrap anything else so translation never raises on
    a tool result shaped like {"ok": ..., "data": ..., "error": ...}."""
    return content if isinstance(content, dict) else {"result": content}


def _is_retryable(exc: Exception) -> bool:
    from google.genai import errors

    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return getattr(exc, "code", None) == 429
    return False


def parse_response(response: Any, latency_ms: int, retry_count: int) -> TurnResult:
    candidate = response.candidates[0]
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for i, part in enumerate(candidate.content.parts or []):
        if getattr(part, "text", None):
            text_parts.append(part.text)
        fc = getattr(part, "function_call", None)
        if fc is not None:
            # Gemini doesn't hand back a call id; synthesize a stable one
            # so tool results can still be correlated turn-locally.
            tool_calls.append(ToolCall(id=f"call_{i}", name=fc.name, arguments=dict(fc.args)))

    stop_reason = "tool_use" if tool_calls else "end_turn"
    usage = response.usage_metadata
    raw = response.model_dump() if hasattr(response, "model_dump") else str(response)

    return TurnResult(
        text="".join(text_parts) or None,
        tool_calls=tuple(tool_calls),
        stop_reason=stop_reason,
        input_tokens=usage.prompt_token_count or 0,
        output_tokens=usage.candidates_token_count or 0,
        latency_ms=latency_ms,
        raw_response=raw,
        retry_count=retry_count,
    )


@dataclass
class GoogleProvider:
    model_id: str
    api_model: str
    api_key: str | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_retries: int = 5
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        system, contents = translate_messages(messages)
        gemini_tools = translate_tools(tools)

        config: dict[str, Any] = {
            "max_output_tokens": self.max_output_tokens,
            "tools": gemini_tools,
        }
        if system is not None:
            config["system_instruction"] = system

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.models.generate_content(
                model=self.api_model, contents=contents, config=config
            )
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        return parse_response(response, elapsed_ms, retry_count)
