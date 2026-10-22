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

# thinking.display "updates" is a beta: "Requires the beta header
# thinking-display-updates-2026-08-18" (without it the value is rejected).
# https://platform.claude.com/docs/en/build-with-claude/thinking#controlling-thinking-display
DISPLAY_UPDATES_BETA = "thinking-display-updates-2026-08-18"
THINKING_DISPLAYS = ("omitted", "summarized", "updates")
# Blocks that carry reasoning state and must go back unmodified.
REASONING_BLOCKS = ("thinking", "redacted_thinking")
# The id given to a verdict that arrived as structured output, not a tool call.
STRUCTURED_OUTPUT_CALL_ID = "structured_output"
# JSON Schema keywords structured outputs don't support ("Numerical
# constraints (such as minimum, maximum, multipleOf)"); stated in the field's
# description instead, and still enforced by whoever reads the result.
_UNSUPPORTED_NUMERIC = ("minimum", "maximum", "multipleOf")


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
            if msg.get("replay") is not None:
                # The response's own content blocks, verbatim: thinking blocks
                # (with their signatures) must be passed back unmodified in
                # tool-use loops on models whose thinking is always on.
                out.append({"role": "assistant", "content": msg["replay"]})
                continue
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


def output_schema(tool: ToolDef) -> dict[str, Any]:
    """The tool's argument schema in the form structured outputs accept:
    objects closed with additionalProperties false, numeric bounds moved into
    the description (the API compiles the schema into a grammar and supports
    no numerical constraints)."""
    schema = tool.json_schema()
    properties = {}
    for name, prop in schema["properties"].items():
        prop = dict(prop)
        bounds = [f"{k} {prop.pop(k)}" for k in _UNSUPPORTED_NUMERIC if k in prop]
        if bounds:
            prop["description"] = f"{prop.get('description', '')} ({', '.join(bounds)})".strip()
        properties[name] = prop
    return {**schema, "properties": properties, "additionalProperties": False}


def served_model_from_raw(raw: Any) -> str | None:
    """The `model` field of a stored Messages API response."""
    return raw.get("model") if isinstance(raw, dict) else None


def parse_response(response: Any, latency_ms: int, retry_count: int, *,
                   progress_updates: bool = False,
                   structured_tool: ToolDef | None = None) -> TurnResult:
    """progress_updates: the request set thinking.display "updates", under
    which "any thinking block with non-empty text is a progress update" -- the
    notes the model writes between tool calls, which models without that
    setting write as text. They are kept in the turn's text, in block order,
    so the transcript shows them as it shows other models' narration.

    structured_tool: the request asked for that tool's arguments as
    structured output; the JSON reply becomes one call to it, or no call at
    all if it doesn't parse (so a caller sees a missing call, not a guess)."""
    segments: list[str] = []
    tool_calls: list[ToolCall] = []
    previous = None
    for block in response.content:
        if block.type == "text":
            if previous == "text" and segments:
                segments[-1] += block.text  # one reply split across blocks
            else:
                segments.append(block.text)
        elif block.type == "thinking" and progress_updates and block.thinking:
            segments.append(block.thinking)
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))
        previous = block.type
    text = "\n\n".join(s for s in segments if s) or None
    if structured_tool is not None and text is not None:
        try:
            arguments = json.loads(text)
        except json.JSONDecodeError:
            arguments = None
        if isinstance(arguments, dict):
            tool_calls.append(ToolCall(id=STRUCTURED_OUTPUT_CALL_ID, name=structured_tool.name,
                                       arguments=arguments))

    raw = response.to_dict()
    return TurnResult(
        text=text,
        tool_calls=tuple(tool_calls),
        stop_reason=response.stop_reason or "end_turn",
        output_capped=response.stop_reason == "max_tokens",
        reasoning_state=any(b.type in REASONING_BLOCKS for b in response.content),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
        raw_response=raw,
        retry_count=retry_count,
        served_model=served_model_from_raw(raw),
        replay=raw.get("content"),
    )


@dataclass
class AnthropicProvider:
    model_id: str
    api_model: str
    api_key: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = 5
    # None sends no temperature, so the provider's default applies. Set for
    # judging (consequence.graders.judge), where it is part of the condition.
    # Claude Opus 4.7 and later reject any non-default value with a 400.
    temperature: float | None = None
    # thinking.display (models.yaml `options:`). None sends no thinking field,
    # which on models whose thinking is always on is equivalent to
    # {"type": "adaptive"}. "updates" returns the model's progress updates
    # between tool calls as text, with reasoning still hidden.
    thinking_display: str | None = None
    # Answer through structured outputs instead of a tool call: run_turn must
    # then be given exactly one tool, whose arguments the reply is
    # constrained to. For models that reject forced tool use (Claude Opus 5.5).
    structured_output: bool = False
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.thinking_display not in (None, *THINKING_DISPLAYS):
            raise ValueError(f"thinking_display must be one of {THINKING_DISPLAYS}, "
                             f"got {self.thinking_display!r}")

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _request(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> dict[str, Any]:
        """The keyword arguments for messages.create(), built without the SDK."""
        system, anthropic_messages = translate_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "max_tokens": self.max_tokens,
            "messages": anthropic_messages,
        }
        if self.structured_output:
            if len(tools) != 1:
                raise ValueError("structured output answers through exactly one tool's "
                                 f"schema; got {len(tools)} tools")
            kwargs["output_config"] = {"format": {"type": "json_schema",
                                                 "schema": output_schema(tools[0])}}
        else:
            kwargs["tools"] = translate_tools(tools)
        if system is not None:
            kwargs["system"] = system
        if self.thinking_display is not None:
            kwargs["thinking"] = {"type": "adaptive", "display": self.thinking_display}
            if self.thinking_display == "updates":
                kwargs["extra_headers"] = {"anthropic-beta": DISPLAY_UPDATES_BETA}
        if self.temperature is not None:
            # The pinned SDK's messages.create() has no temperature parameter
            # (passing one raises TypeError before any request), so it goes in
            # the request body directly.
            kwargs["extra_body"] = {"temperature": self.temperature}
        return kwargs

    def run_turn(self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]) -> TurnResult:
        client = self._get_client()
        kwargs = self._request(messages, tools)

        def _call() -> tuple[Any, int]:
            start = time.monotonic()
            response = client.messages.create(**kwargs)
            return response, int((time.monotonic() - start) * 1000)

        (response, elapsed_ms), retry_count = call_with_backoff(
            _call, _is_retryable, max_retries=self.max_retries
        )
        return parse_response(response, elapsed_ms, retry_count,
                              progress_updates=self.thinking_display == "updates",
                              structured_tool=tools[0] if self.structured_output else None)
