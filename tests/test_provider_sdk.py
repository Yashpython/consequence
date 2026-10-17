"""Every smoke-test call, made offline through the real vendor SDKs.

Each adapter is built exactly as the smoke tests build it (and so as `run`
and `grade` do), its SDK client is given a fake HTTP transport that records
the request and returns a canned response, and the call goes through the
SDK's own argument handling and serialization. What this catches without
spending anything: a keyword the pinned SDK doesn't accept (the Anthropic
SDK has no `temperature` parameter: passing it raised TypeError before any
request), a request field in the wrong place, a beta header not sent, a reply
the adapter can't parse, a replay that doesn't go back verbatim.

What it can't catch is the vendor refusing a well-formed request, which is
what the billed smoke tests (tests/test_smoke_providers.py) are for.

Needs the vendor SDKs (`pip install -e ".[dev,providers]"`, as CI does).
"""

from __future__ import annotations

import json

import pytest

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")
genai = pytest.importorskip("google.genai")
httpx = pytest.importorskip("httpx")
httpx2 = pytest.importorskip("httpx2")

from test_smoke_providers import (
    JUDGES,
    MODELS,
    TINY_TRANSCRIPT,
    agent_messages,
    build_judge,
    build_model,
    judge_conditions,
    with_tool_result,
)

from consequence.graders.judge import build_messages, parse_vote
from consequence.graders.judge_prompt import VERDICT_TOOL
from consequence.providers import anthropic as anthropic_adapter
from consequence.providers.openrouter import OPENROUTER_BASE_URL
from consequence.tools.schema import TOOLS

VERDICT = {"passed": True, "confidence": 0.9, "reasoning": "void_invoice returned ok",
           "behaviour": "completed"}
PROGRESS_UPDATE = "Found invoice 12; checking its status next."


class Wire:
    """A fake transport: records each request, answers from a script."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, module):
        def handle(request):
            self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                                  "body": json.loads(request.content or b"{}")})
            return module.Response(200, json=self.responses.pop(0))
        return handle


def install(provider, provider_name: str, wire: Wire) -> None:
    """Give the adapter a real SDK client whose transport is `wire`."""
    if provider_name == "anthropic":
        client = anthropic.Anthropic(
            api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(
                wire.handler(httpx2))))
    elif provider_name in ("openai", "openrouter"):
        base_url = OPENROUTER_BASE_URL if provider_name == "openrouter" else None
        client = openai.OpenAI(
            api_key="test", base_url=base_url,
            http_client=httpx2.Client(transport=httpx2.MockTransport(wire.handler(httpx2))))
    elif provider_name == "google":
        client = genai.Client(api_key="test", http_options=genai.types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(wire.handler(httpx)))))
    else:
        raise AssertionError(f"no fake transport for {provider_name}")
    provider._client = client


# -- canned responses, in each vendor's wire format ---------------------------------------


def anthropic_reply(model: str, content: list[dict], stop: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 40}}


def openai_reply(model: str, message: dict, finish: str) -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}}


def openai_call(name: str, arguments: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def google_reply(model: str, parts: list[dict]) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": parts},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30,
                              "thoughtsTokenCount": 10},
            "modelVersion": model}


def judge_reply(spec) -> dict:
    if spec.provider == "anthropic":
        if spec.options.get("structured_output"):
            return anthropic_reply(spec.api_model, [
                {"type": "thinking", "thinking": "", "signature": "sig-judge"},
                {"type": "text", "text": json.dumps(VERDICT)}], "end_turn")
        return anthropic_reply(spec.api_model, [{"type": "tool_use", "id": "tu_v",
                                                 "name": VERDICT_TOOL.name,
                                                 "input": VERDICT}], "tool_use")
    if spec.provider in ("openai", "openrouter"):
        return openai_reply(spec.api_model, openai_call(VERDICT_TOOL.name, VERDICT),
                            "tool_calls")
    if spec.provider == "google":
        return google_reply(spec.api_model, [{"functionCall": {"name": VERDICT_TOOL.name,
                                                               "args": VERDICT}}])
    raise AssertionError(spec.provider)


def agent_replies(spec) -> list[dict]:
    """A tool call (with whatever reasoning state the vendor attaches), then
    a closing text."""
    args = {"invoice_id": 12}
    if spec.provider == "anthropic":
        return [anthropic_reply(spec.api_model, [
                    {"type": "thinking", "thinking": "", "signature": "sig-reasoning"},
                    {"type": "thinking", "thinking": PROGRESS_UPDATE, "signature": "sig-update"},
                    {"type": "tool_use", "id": "tu_1", "name": "get_invoice", "input": args}],
                    "tool_use"),
                anthropic_reply(spec.api_model, [{"type": "text", "text": "It is active."}],
                                "end_turn")]
    if spec.provider in ("openai", "openrouter"):
        return [openai_reply(spec.api_model, openai_call("get_invoice", args), "tool_calls"),
                openai_reply(spec.api_model, {"role": "assistant", "content": "It is active."},
                             "stop")]
    if spec.provider == "google":
        return [google_reply(spec.api_model, [{"functionCall": {"name": "get_invoice",
                                                                "args": args},
                                               "thoughtSignature": "c2lnLWdlbWluaQ=="}]),
                google_reply(spec.api_model, [{"text": "It is active."}])]
    raise AssertionError(spec.provider)


def _sent_temperature(spec, body: dict):
    if spec.provider == "google":
        return body.get("generationConfig", {}).get("temperature")
    return body.get("temperature")


# -- every smoke-test call, offline -------------------------------------------------------


@pytest.mark.parametrize("condition", judge_conditions(),
                         ids=lambda c: f"{c[0]}-{'default' if c[1] is None else c[1]}")
def test_each_judge_condition_goes_through_its_sdk(condition):
    judge_id, temperature = condition
    spec = JUDGES[judge_id]
    provider = build_judge(judge_id, temperature)
    wire = Wire([judge_reply(spec)])
    install(provider, spec.provider, wire)

    turn = provider.run_turn(build_messages(TINY_TRANSCRIPT), (VERDICT_TOOL,))

    assert parse_vote(turn).behaviour == "completed"
    assert turn.served_model == spec.api_model
    body = wire.requests[0]["body"]
    assert body["model"] == spec.api_model
    assert _sent_temperature(spec, body) == temperature  # None: not sent at all
    assert "tool_choice" not in body  # never forced: Opus 5.5 rejects forced tool use
    if spec.options.get("structured_output"):
        assert "tools" not in body
        assert body["output_config"]["format"]["type"] == "json_schema"
    if spec.max_output_tokens is not None:
        assert spec.max_output_tokens in (body.get("max_tokens"),
                                          body.get("max_completion_tokens"))


@pytest.mark.parametrize("model_id", sorted(MODELS))
def test_each_model_s_two_turns_go_through_its_sdk_and_replay_verbatim(model_id):
    spec = MODELS[model_id]
    provider = build_model(model_id)
    wire = Wire(agent_replies(spec))
    install(provider, spec.provider, wire)

    messages = agent_messages()
    turn = provider.run_turn(messages, TOOLS)
    assert [c.name for c in turn.tool_calls] == ["get_invoice"]
    assert turn.served_model and turn.input_tokens > 0 and turn.output_tokens > 0
    second = provider.run_turn(with_tool_result(messages, turn), TOOLS)
    assert second.text == "It is active."

    first_body, second_body = (r["body"] for r in wire.requests)
    if spec.provider == "openrouter":
        assert wire.requests[0]["url"].startswith(OPENROUTER_BASE_URL)
    assert "temperature" not in json.dumps(first_body)  # agents run at the provider default
    if spec.provider == "anthropic":
        # The reply's own blocks go back unchanged, signatures and all.
        assert second_body["messages"][1] == {"role": "assistant",
                                              "content": agent_replies(spec)[0]["content"]}
    if spec.provider == "google":
        sent = second_body["contents"][1]["parts"][0]
        assert sent["thoughtSignature"] == "c2lnLWdlbWluaQ=="


def test_opus_5_5_as_an_agent_returns_its_progress_updates_as_narration():
    """thinking.display "updates" with its beta header; the progress update
    becomes the turn's text, as other models' notes between tool calls are."""
    spec = MODELS["claude-opus-5.5"]
    provider = build_model("claude-opus-5.5")
    wire = Wire(agent_replies(spec))
    install(provider, spec.provider, wire)

    turn = provider.run_turn(agent_messages(), TOOLS)

    request = wire.requests[0]
    assert request["body"]["thinking"] == {"type": "adaptive", "display": "updates"}
    assert anthropic_adapter.DISPLAY_UPDATES_BETA in request["headers"]["anthropic-beta"]
    assert turn.text == PROGRESS_UPDATE  # the empty reasoning block adds nothing


def test_an_anthropic_temperature_reaches_the_request_body():
    """The regression: the pinned SDK's create() has no temperature
    parameter, so passing it as a keyword raised TypeError before any
    request. It must arrive in the body instead."""
    provider = anthropic_adapter.AnthropicProvider(model_id="m", api_model="claude-opus-4-6",
                                                   temperature=0.0)
    wire = Wire([anthropic_reply("claude-opus-4-6", [{"type": "text", "text": "ok"}],
                                 "end_turn")])
    install(provider, "anthropic", wire)
    provider.run_turn(agent_messages(), TOOLS)
    assert wire.requests[0]["body"]["temperature"] == 0.0


def test_a_structured_verdict_that_is_not_json_is_no_verdict():
    """The JSON reply becomes the verdict call; anything else becomes no
    call at all, which parse_vote records as malformed, never a guess."""
    spec = JUDGES["judge-claude-opus-5.5"]
    provider = build_judge(spec.id, None)
    wire = Wire([anthropic_reply(spec.api_model, [{"type": "text", "text": "I think it passed."}],
                                 "end_turn")])
    install(provider, spec.provider, wire)
    turn = provider.run_turn(build_messages(TINY_TRANSCRIPT), (VERDICT_TOOL,))
    assert turn.tool_calls == () and turn.text == "I think it passed."


def _sent_cap(spec, body: dict):
    if spec.provider == "google":
        return body.get("generationConfig", {}).get("maxOutputTokens")
    return body.get("max_tokens", body.get("max_completion_tokens"))


@pytest.mark.parametrize("model_id", sorted(MODELS))
def test_every_model_s_request_carries_the_uniform_agent_cap(model_id):
    spec = MODELS[model_id]
    provider = build_model(model_id)
    wire = Wire(agent_replies(spec)[:1])
    install(provider, spec.provider, wire)
    provider.run_turn(agent_messages(), TOOLS)
    assert _sent_cap(spec, wire.requests[0]["body"]) == spec.max_output_tokens == 16_000


def test_a_gemini_turn_cut_off_by_the_cap_is_flagged_through_the_real_sdk():
    """All the cap spent on thinking: no parts, finishReason MAX_TOKENS."""
    spec = MODELS["gemini-4-argon"]
    provider = build_model(spec.id)
    reply = google_reply(spec.api_model, [])
    reply["candidates"][0]["finishReason"] = "MAX_TOKENS"
    wire = Wire([reply])
    install(provider, spec.provider, wire)
    turn = provider.run_turn(agent_messages(), TOOLS)
    assert (turn.stop_reason, turn.output_capped, turn.tool_calls) == ("MAX_TOKENS", True, ())
