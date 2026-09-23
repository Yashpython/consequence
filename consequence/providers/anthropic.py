"""Anthropic Messages API adapter (POST /v1/messages, client-defined tools)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from consequence.providers.base import ToolCall
from consequence.providers.common import (
    HTTPProvider,
    ParsedTurn,
    Usage,
    split_system,
    tool_result_text,
)
from consequence.tools.schema import ToolDef

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def _is_error(content: Any) -> bool:
    return isinstance(content, Mapping) and content.get("ok") is False


class AnthropicProvider(HTTPProvider):
    provider_name = "anthropic"

    def url(self) -> str:
        return API_URL

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    @staticmethod
    def tool_to_native(tool: ToolDef) -> dict[str, Any]:
        return {"name": tool.name, "description": tool.description,
                "input_schema": tool.json_schema()}

    def translate_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                # Replay the exact blocks we received (thinking blocks carry
                # signatures the API requires back alongside tool_use).
                content = self.native_for(msg)
                if content is None:
                    content = []
                    if msg.get("content"):
                        content.append({"type": "text", "text": msg["content"]})
                    for call in msg.get("tool_calls") or []:
                        content.append({"type": "tool_use", "id": call["id"],
                                        "name": call["name"], "input": call["arguments"]})
                out.append({"role": "assistant", "content": content})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"],
                         "content": tool_result_text(msg["content"])}
                if _is_error(msg["content"]):
                    block["is_error"] = True
                # All results for one assistant turn go back in one user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                raise ValueError(f"unexpected role {role!r}")
        return out

    def build_request(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> dict[str, Any]:
        system, rest = split_system(messages)
        body: dict[str, Any] = {
            "model": self.spec.api_model,
            "max_tokens": self.spec.max_output_tokens,
            "messages": self.translate_messages(rest),
        }
        if system is not None:
            body["system"] = system
        if tools:
            body["tools"] = [self.tool_to_native(t) for t in tools]
        body.update(self.spec.params)
        return body

    def parse_response(self, payload: Mapping[str, Any]) -> ParsedTurn:
        blocks = payload.get("content") or []
        texts = [b["text"] for b in blocks if b.get("type") == "text"]
        calls = tuple(
            ToolCall(id=b["id"], name=b["name"], arguments=dict(b.get("input") or {}))
            for b in blocks
            if b.get("type") == "tool_use"
        )
        usage = payload.get("usage") or {}
        return ParsedTurn(
            text="".join(texts) if texts else None,
            tool_calls=calls,
            stop_reason=payload.get("stop_reason") or "unknown",
            usage=Usage(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cached_input_tokens=usage.get("cache_read_input_tokens") or 0,
                cache_write_tokens=usage.get("cache_creation_input_tokens") or 0,
            ),
            native_assistant=blocks,
        )
