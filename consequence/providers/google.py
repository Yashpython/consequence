"""Google Gemini API adapter (generateContent, functionDeclarations)."""

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
    split_system,
)
from consequence.tools.schema import ToolDef

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Gemini's `parameters` field takes its OpenAPI-subset Schema object, not
# arbitrary JSON Schema: types are the upper-case enum names and there is no
# additionalProperties. Only these keys carry over.
_SCHEMA_KEYS = ("description", "enum", "minimum", "maximum", "required")


def to_gemini_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": str(schema["type"]).upper()}
    for key in _SCHEMA_KEYS:
        if key in schema:
            out[key] = schema[key]
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


def _response_object(content: Any) -> dict[str, Any]:
    """functionResponse.response must be a JSON object; round-trip through
    JSON so non-JSON values (dates) are sent exactly as the other adapters
    send them."""
    value = json.loads(content) if isinstance(content, str) else content
    value = json.loads(json.dumps(value, default=str))
    return value if isinstance(value, dict) else {"result": value}


class GoogleProvider(HTTPProvider):
    provider_name = "google"

    def url(self) -> str:
        return f"{API_BASE}/{self.spec.api_model}:generateContent"

    def headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "content-type": "application/json"}

    @staticmethod
    def tool_to_native(tool: ToolDef) -> dict[str, Any]:
        return {"name": tool.name, "description": tool.description,
                "parameters": to_gemini_schema(tool.json_schema())}

    def translate_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "parts": [{"text": msg["content"]}]})
            elif role == "assistant":
                # Replay the exact parts we received: Gemini requires function
                # call parts' thoughtSignature back on the next request.
                parts = self.native_for(msg)
                if parts is None:
                    parts = []
                    if msg.get("content"):
                        parts.append({"text": msg["content"]})
                    for call in msg.get("tool_calls") or []:
                        fc: dict[str, Any] = {"name": call["name"], "args": call["arguments"]}
                        if not call["id"].startswith("local-call-"):
                            fc["id"] = call["id"]
                        parts.append({"functionCall": fc})
                out.append({"role": "model", "parts": parts})
            elif role == "tool":
                fr: dict[str, Any] = {"name": msg["name"],
                                      "response": _response_object(msg["content"])}
                if not msg["tool_call_id"].startswith("local-call-"):
                    fr["id"] = msg["tool_call_id"]
                part = {"functionResponse": fr}
                if out and out[-1]["role"] == "user" and "functionResponse" in out[-1]["parts"][0]:
                    out[-1]["parts"].append(part)
                else:
                    out.append({"role": "user", "parts": [part]})
            else:
                raise ValueError(f"unexpected role {role!r}")
        return out

    def build_request(
        self, messages: list[dict[str, Any]], tools: Sequence[ToolDef]
    ) -> dict[str, Any]:
        system, rest = split_system(messages)
        params = dict(self.spec.params)
        generation_config = {"maxOutputTokens": self.spec.max_output_tokens,
                             **params.pop("generationConfig", {})}
        body: dict[str, Any] = {
            "contents": self.translate_messages(rest),
            "generationConfig": generation_config,
        }
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [self.tool_to_native(t) for t in tools]}]
        body.update(params)
        return body

    def parse_response(self, payload: Mapping[str, Any]) -> ParsedTurn:
        meta = payload.get("usageMetadata") or {}
        prompt = (meta.get("promptTokenCount") or 0) + (meta.get("toolUsePromptTokenCount") or 0)
        cached = meta.get("cachedContentTokenCount") or 0
        usage = Usage(
            input_tokens=prompt - cached,
            # Thinking tokens are billed as output but reported separately.
            output_tokens=(meta.get("candidatesTokenCount") or 0)
            + (meta.get("thoughtsTokenCount") or 0),
            cached_input_tokens=cached,
        )

        candidates = payload.get("candidates") or []
        if not candidates:
            block = (payload.get("promptFeedback") or {}).get("blockReason")
            if block is None:
                raise ProviderResponseError("response has no candidates")
            return ParsedTurn(text=None, tool_calls=(), stop_reason=f"blocked:{block}",
                              usage=usage, native_assistant=[])

        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        texts = [p["text"] for p in parts if "text" in p and not p.get("thought")]
        calls = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(id=fc.get("id") or self.new_call_id(), name=fc["name"],
                                      arguments=dict(fc.get("args") or {})))
        return ParsedTurn(
            text="".join(texts) if texts else None,
            tool_calls=tuple(calls),
            stop_reason=candidate.get("finishReason") or "unknown",
            usage=usage,
            native_assistant=parts,
        )

    def served_model(self, payload: Mapping[str, Any]) -> str | None:
        return payload.get("modelVersion")
