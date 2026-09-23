"""Provider adapter tests.

Everything except the @pytest.mark.smoke tests is offline: HTTP goes through
httpx.MockTransport, so no network call is made and nothing is spent. The
smoke tests make one real, trivial call per provider to confirm credentials
and the pinned model string; they're skipped by default (`make test-smoke`).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from consequence import harness
from consequence.environment import Snapshot
from consequence.harness import Task
from consequence.providers.anthropic import AnthropicProvider
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.common import (
    EpisodeUsage,
    MissingPriceError,
    PriceTier,
    Pricing,
    ProviderHTTPError,
    RetryPolicy,
    Usage,
    compute_cost,
)
from consequence.providers.google import GoogleProvider
from consequence.providers.openai import UNPARSEABLE_ARGUMENTS_KEY, OpenAIProvider
from consequence.providers.openrouter import OpenRouterProvider
from consequence.providers.registry import (
    RegistryError,
    build_provider,
    load_registry,
    parse_entry,
)
from consequence.results import Results
from consequence.runner import run_recorded_episode
from consequence.tools.schema import TOOLS, ToolDef, ToolParameter

FIXTURES = Path(__file__).parent / "fixtures" / "providers"

ADAPTERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
    "openrouter": OpenRouterProvider,
}

FIXTURE_TOOL = ToolDef(
    name="adjust_invoice",
    description="Fixture tool: adjust one invoice.",
    parameters=(
        ToolParameter("invoice_id", "integer", "Invoice id", required=True, minimum=1),
        ToolParameter("status", "string", "New status", enum=("active", "void")),
        ToolParameter("amount", "number", "Amount", minimum=0, maximum=1000),
        ToolParameter("notify", "boolean"),
    ),
)

CONVERSATION = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "Void invoices 2 and 3."},
    {
        "role": "assistant",
        "content": "Voiding both.",
        "tool_calls": [
            {"id": "c1", "name": "void_invoice", "arguments": {"invoice_id": 2, "reason": "r"}},
            {"id": "c2", "name": "void_invoice", "arguments": {"invoice_id": 3, "reason": "r"}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "name": "void_invoice",
     "content": {"ok": True, "data": {"invoice_id": 2, "status": "void"}, "error": None}},
    {"role": "tool", "tool_call_id": "c2", "name": "void_invoice",
     "content": {"ok": False, "data": None, "error": "invoice 3 is paid"}},
]


def _spec(provider: str, **overrides):
    raw = {
        "id": f"test-{provider}",
        "provider": provider,
        "api_model": {
            "anthropic": "claude-test-1",
            "openai": "gpt-test-2026-01-01",
            "google": "gemini-test",
            "openrouter": "lab/open-model",
        }[provider],
        "input_price_per_mtok": 2.0,
        "cached_input_price_per_mtok": 0.2,
        "output_price_per_mtok": 10.0,
        "max_output_tokens": 1024,
    }
    if provider == "openrouter":
        raw["routing"] = {"order": ["somehost"], "allow_fallbacks": False}
    raw.update(overrides)
    return parse_entry(raw)


class Recorder:
    """An httpx transport that replays scripted responses and keeps requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def _provider(provider: str, responses, *, sleeps=None, **spec_overrides):
    recorder = Recorder(responses)
    policy = RetryPolicy(
        max_retries=3,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        jitter=lambda: 1.0,
    )
    adapter = ADAPTERS[provider](
        _spec(provider, **spec_overrides),
        "test-key",
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        retry=policy,
    )
    return adapter, recorder


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode(),
                          headers={"content-type": "application/json"})


# -- canned provider responses (offline, shaped per each API's docs) --------

ANTHROPIC_TOOL_TURN = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test-1",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Voiding."},
        {"type": "tool_use", "id": "toolu_1", "name": "void_invoice",
         "input": {"invoice_id": 2, "reason": "dup"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 1200, "output_tokens": 80,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
}
ANTHROPIC_FINAL_TURN = {
    "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-test-1",
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1400, "output_tokens": 5},
}
OPENAI_TOOL_TURN = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-test-2026-01-01",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant", "content": None, "refusal": None, "annotations": [],
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "void_invoice",
                                         "arguments": "{\"invoice_id\": 2, \"reason\": \"dup\"}"}}],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 3000, "completion_tokens": 400,
              "prompt_tokens_details": {"cached_tokens": 1024},
              "completion_tokens_details": {"reasoning_tokens": 300}},
}
OPENROUTER_TOOL_TURN = {
    "id": "gen-1", "model": "lab/open-model", "provider": "somehost",
    "choices": [{
        "message": {
            "role": "assistant", "content": "", "reasoning": "think",
            "reasoning_details": [{"type": "reasoning.text", "text": "think"}],
            "tool_calls": [{"id": "call_9", "type": "function",
                            "function": {"name": "get_invoice", "arguments": "{not json"}}],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 500, "completion_tokens": 50, "cost": 0.0015},
}
GOOGLE_TOOL_TURN = {
    "candidates": [{
        "content": {"role": "model", "parts": [
            {"text": "planning", "thought": True},
            {"functionCall": {"name": "void_invoice", "args": {"invoice_id": 2, "reason": "dup"}},
             "thoughtSignature": "gsig-1"},
        ]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 2000, "candidatesTokenCount": 30,
                      "thoughtsTokenCount": 170, "cachedContentTokenCount": 500},
    "modelVersion": "gemini-test",
}


# -- tool schema translation, per provider, against fixtures ----------------


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_tool_schema_translation_matches_fixture(provider):
    expected = json.loads((FIXTURES / f"tool_{provider}.json").read_text())
    assert ADAPTERS[provider].tool_to_native(FIXTURE_TOOL) == expected


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_every_real_tool_translates_and_keeps_descriptions_byte_identical(provider):
    for tool in TOOLS:
        native = json.dumps(ADAPTERS[provider].tool_to_native(tool))
        assert json.dumps(tool.description)[1:-1] in native
        for param in tool.parameters:
            assert json.dumps(param.description)[1:-1] in native


def test_google_schema_drops_unsupported_keys():
    for tool in TOOLS:
        params = GoogleProvider.tool_to_native(tool)["parameters"]
        assert "additionalProperties" not in json.dumps(params)
        assert params["type"] == "OBJECT"


def test_tools_key_is_omitted_when_there_are_no_tools():
    for provider in ADAPTERS:
        adapter, _ = _provider(provider, [])
        assert "tools" not in adapter.build_request(CONVERSATION[:2], ())


# -- conversation translation ------------------------------------------------


def test_anthropic_messages_translation():
    adapter, _ = _provider("anthropic", [])
    body = adapter.build_request(CONVERSATION, [FIXTURE_TOOL])
    assert body["system"] == "SYS"
    assert body["max_tokens"] == 1024
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1] == {
        "type": "tool_use", "id": "c1", "name": "void_invoice",
        "input": {"invoice_id": 2, "reason": "r"},
    }
    results = body["messages"][2]["content"]  # both results in ONE user message
    assert [r["tool_use_id"] for r in results] == ["c1", "c2"]
    assert "is_error" not in results[0] and results[1]["is_error"] is True
    assert json.loads(results[0]["content"])["data"]["status"] == "void"


def test_openai_messages_translation():
    adapter, _ = _provider("openai", [])
    body = adapter.build_request(CONVERSATION, [FIXTURE_TOOL])
    assert body["max_completion_tokens"] == 1024
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {
        "invoice_id": 2, "reason": "r"}
    assert msgs[3]["tool_call_id"] == "c1"


def test_google_messages_translation():
    adapter, _ = _provider("google", [])
    body = adapter.build_request(CONVERSATION, [FIXTURE_TOOL])
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["generationConfig"]["maxOutputTokens"] == 1024
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    responses = body["contents"][2]["parts"]
    assert [p["functionResponse"]["name"] for p in responses] == ["void_invoice"] * 2
    assert responses[1]["functionResponse"]["response"]["error"] == "invoice 3 is paid"
    assert body["tools"] == [{"functionDeclarations": [GoogleProvider.tool_to_native(FIXTURE_TOOL)]}]


def test_openrouter_request_pins_routing_and_asks_for_cost():
    adapter, _ = _provider("openrouter", [])
    body = adapter.build_request(CONVERSATION, [FIXTURE_TOOL])
    assert body["provider"] == {"order": ["somehost"], "allow_fallbacks": False}
    assert body["usage"] == {"include": True}
    assert body["max_tokens"] == 1024


def test_registry_params_are_merged_into_request():
    adapter, _ = _provider("anthropic", [], params={"output_config": {"effort": "high"}})
    assert adapter.build_request(CONVERSATION[:2], ())["output_config"] == {"effort": "high"}


# -- response translation back into TurnResult --------------------------------


def test_anthropic_response_to_turn_result():
    adapter, recorder = _provider("anthropic", [_json_response(ANTHROPIC_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    assert turn.text == "Voiding."
    assert turn.tool_calls == (ToolCall("toolu_1", "void_invoice", {"invoice_id": 2, "reason": "dup"}),)
    assert turn.stop_reason == "tool_use"
    assert (turn.input_tokens, turn.output_tokens) == (1200, 80)
    assert turn.cost_usd == pytest.approx((1200 * 2 + 80 * 10) / 1e6)
    assert json.loads(turn.raw_response) == ANTHROPIC_TOOL_TURN
    assert recorder.requests[0].headers["x-api-key"] == "test-key"


def test_openai_response_to_turn_result_splits_cached_tokens():
    adapter, _ = _provider("openai", [_json_response(OPENAI_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    assert turn.text is None
    assert turn.tool_calls[0].arguments == {"invoice_id": 2, "reason": "dup"}
    assert (turn.input_tokens, turn.output_tokens) == (3000, 400)
    # 1976 uncached @ $2, 1024 cached @ $0.20, 400 output (incl. reasoning) @ $10
    assert turn.cost_usd == pytest.approx((1976 * 2 + 1024 * 0.2 + 400 * 10) / 1e6)


def test_openrouter_unparseable_arguments_are_kept_not_dropped():
    adapter, _ = _provider("openrouter", [_json_response(OPENROUTER_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    assert turn.tool_calls[0].arguments == {UNPARSEABLE_ARGUMENTS_KEY: "{not json"}


def test_google_response_to_turn_result_counts_thinking_as_output():
    adapter, _ = _provider("google", [_json_response(GOOGLE_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    assert turn.text is None  # the thought part is not answer text
    assert turn.tool_calls[0].name == "void_invoice"
    assert turn.tool_calls[0].id.startswith("local-call-")
    assert (turn.input_tokens, turn.output_tokens) == (2000, 200)
    assert turn.cost_usd == pytest.approx((1500 * 2 + 500 * 0.2 + 200 * 10) / 1e6)


def test_raw_response_is_the_exact_body_bytes():
    body = b'{"id":"msg_x", "content":[{"type":"text","text":"hi"}],  "stop_reason":"end_turn",' \
           b'"usage":{"input_tokens":1,"output_tokens":1}}'
    adapter, _ = _provider("anthropic", [httpx.Response(200, content=body)])
    assert adapter.run_turn(CONVERSATION[:2], ()).raw_response == body.decode()


# -- native replay of provider-only state --------------------------------------


def _continue(adapter, turn: TurnResult) -> list[dict]:
    """The neutral history the harness would build after executing `turn`."""
    return CONVERSATION[:2] + [
        {"role": "assistant", "content": turn.text,
         "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in turn.tool_calls]},
        *[{"role": "tool", "tool_call_id": c.id, "name": c.name,
           "content": {"ok": True, "data": {}, "error": None}} for c in turn.tool_calls],
    ]


def test_anthropic_replays_thinking_block_signatures():
    adapter, recorder = _provider("anthropic", [_json_response(ANTHROPIC_TOOL_TURN),
                                                _json_response(ANTHROPIC_FINAL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    adapter.run_turn(_continue(adapter, turn), TOOLS)
    assert recorder.body()["messages"][1]["content"] == ANTHROPIC_TOOL_TURN["content"]


def test_google_replays_thought_signatures():
    final = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]},
                             "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}
    adapter, recorder = _provider("google", [_json_response(GOOGLE_TOOL_TURN),
                                             _json_response(final)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    adapter.run_turn(_continue(adapter, turn), TOOLS)
    contents = recorder.body()["contents"]
    assert contents[1]["parts"] == GOOGLE_TOOL_TURN["candidates"][0]["content"]["parts"]
    # a locally assigned id is never sent to Gemini
    assert "id" not in contents[2]["parts"][0]["functionResponse"]


def test_openrouter_replays_reasoning_details_but_not_output_only_fields():
    adapter, recorder = _provider("openrouter", [_json_response(OPENROUTER_TOOL_TURN),
                                                 _json_response(OPENROUTER_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    adapter.run_turn(_continue(adapter, turn), TOOLS)
    replayed = recorder.body()["messages"][2]
    assert replayed["reasoning_details"] == [{"type": "reasoning.text", "text": "think"}]
    assert replayed["tool_calls"][0]["function"]["arguments"] == "{not json"
    assert "reasoning" not in replayed


def test_openai_replay_drops_output_only_fields():
    adapter, recorder = _provider("openai", [_json_response(OPENAI_TOOL_TURN),
                                             _json_response(OPENAI_TOOL_TURN)])
    turn = adapter.run_turn(CONVERSATION[:2], TOOLS)
    adapter.run_turn(_continue(adapter, turn), TOOLS)
    assert set(recorder.body()["messages"][2]) == {"role", "content", "tool_calls"}


# -- retry policy ----------------------------------------------------------------


def test_retries_429_and_5xx_with_exponential_backoff():
    sleeps: list[float] = []
    adapter, recorder = _provider(
        "anthropic",
        [httpx.Response(429, text="slow down"), httpx.Response(503, text="overloaded"),
         httpx.Response(529, text="overloaded"), _json_response(ANTHROPIC_FINAL_TURN)],
        sleeps=sleeps,
    )
    turn = adapter.run_turn(CONVERSATION[:2], ())
    assert turn.retries == 3
    assert len(recorder.requests) == 4
    assert sleeps == [2.0, 4.0, 8.0]
    assert adapter.finish_episode().retry_count == 3


def test_retry_after_header_is_honoured_as_a_floor():
    sleeps: list[float] = []
    adapter, _ = _provider(
        "openai",
        [httpx.Response(429, headers={"retry-after": "30"}), _json_response(OPENAI_TOOL_TURN)],
        sleeps=sleeps,
    )
    adapter.run_turn(CONVERSATION[:2], ())
    assert sleeps == [30.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_other_4xx_is_never_retried(status):
    adapter, recorder = _provider("openai", [httpx.Response(status, text="bad")])
    with pytest.raises(ProviderHTTPError) as exc:
        adapter.run_turn(CONVERSATION[:2], ())
    assert exc.value.status_code == status and exc.value.retries == 0
    assert len(recorder.requests) == 1


def test_transport_error_is_never_retried():
    # A timeout may mean the turn completed (and was billed) server-side.
    adapter, recorder = _provider("google", [httpx.ReadTimeout("timed out"),
                                             _json_response(GOOGLE_TOOL_TURN)])
    with pytest.raises(httpx.ReadTimeout):
        adapter.run_turn(CONVERSATION[:2], ())
    assert len(recorder.requests) == 1


def test_completed_turn_is_never_retried_even_if_unusable():
    adapter, recorder = _provider("openai", [_json_response({"choices": []}),
                                             _json_response(OPENAI_TOOL_TURN)])
    with pytest.raises(RuntimeError, match="no choices"):
        adapter.run_turn(CONVERSATION[:2], ())
    assert len(recorder.requests) == 1


def test_exhausted_retries_raise_and_still_count():
    adapter, recorder = _provider("anthropic", [httpx.Response(500)] * 4)
    with pytest.raises(ProviderHTTPError) as exc:
        adapter.run_turn(CONVERSATION[:2], ())
    assert exc.value.retries == 3 and len(recorder.requests) == 4
    usage = adapter.finish_episode()
    assert usage.retry_count == 3 and usage.turns == ()


# -- cost --------------------------------------------------------------------------


def _pricing(inp, out, cached=None, write=None, **long):
    tier = PriceTier(Decimal(str(inp)), Decimal(str(out)),
                     None if cached is None else Decimal(str(cached)),
                     None if write is None else Decimal(str(write)))
    if not long:
        return Pricing(base=tier)
    return Pricing(
        base=tier,
        long_context=PriceTier(Decimal(str(long["l_in"])), Decimal(str(long["l_out"])),
                               Decimal(str(long["l_cached"]))),
        long_context_threshold=long["threshold"],
    )


def test_cost_known_tokens_known_prices():
    # 1,234 in @ $5/M + 567 out @ $25/M = 0.00617 + 0.014175
    assert compute_cost(_pricing(5, 25), Usage(1234, 567)) == 0.020345


def test_cost_one_million_tokens_is_the_list_price():
    assert compute_cost(_pricing(5, 25), Usage(1_000_000, 0)) == 5.0
    assert compute_cost(_pricing(5, 25), Usage(0, 1_000_000)) == 25.0


def test_cost_with_cached_input():
    # 6,000 uncached @ $5 + 4,000 cached @ $0.50 + 2,000 out @ $30
    assert compute_cost(_pricing(5, 30, cached=0.5), Usage(6000, 2000, 4000)) == 0.092


def test_cost_with_cache_writes():
    # 100 @ $3 + 1,000 written @ $3.75 + 10 out @ $15
    assert compute_cost(_pricing(3, 15, write=3.75), Usage(100, 10, cache_write_tokens=1000)) \
        == 0.0042


def test_long_context_tier_applies_to_the_whole_request_above_threshold():
    p = _pricing(2, 12, cached=0.2, l_in=4, l_out=18, l_cached=0.4, threshold=200_000)
    assert compute_cost(p, Usage(200_000, 1000)) == 0.412  # at threshold: base tier
    assert compute_cost(p, Usage(250_000, 1000)) == 1.018  # above: all of it at long tier
    # cached tokens count toward the threshold
    assert compute_cost(p, Usage(150_000, 0, 60_000)) == 0.624


def test_cost_for_unpriced_tokens_is_an_error_not_an_estimate():
    with pytest.raises(MissingPriceError):
        compute_cost(_pricing(1, 5), Usage(10, 10, cached_input_tokens=5))


def test_turn_with_unpriced_tokens_has_unknown_cost_and_so_does_the_episode():
    payload = dict(ANTHROPIC_FINAL_TURN, usage={"input_tokens": 10, "output_tokens": 1,
                                                "cache_creation_input_tokens": 500})
    adapter, _ = _provider("anthropic", [_json_response(payload),
                                         _json_response(ANTHROPIC_FINAL_TURN)])
    assert adapter.run_turn(CONVERSATION[:2], ()).cost_usd is None
    assert adapter.run_turn(CONVERSATION[:2], ()).cost_usd is not None
    assert adapter.finish_episode().cost_usd is None


def test_episode_cost_is_the_exact_sum_of_turn_costs():
    turns = tuple(TurnResult(text=None, cost_usd=c) for c in (0.1, 0.2, 0.000001))
    assert EpisodeUsage("m", turns, 0).cost_usd == 0.300001


def test_registry_prices_drive_cost():
    specs = load_registry()
    assert compute_cost(specs["claude-haiku-4.5"].pricing, Usage(1000, 500)) == 0.0035
    assert compute_cost(specs["claude-opus-5"].pricing, Usage(1000, 500)) == 0.0175


# -- the registry ------------------------------------------------------------------


def test_registry_has_five_pinned_models_across_all_four_providers():
    specs = load_registry()
    assert len(specs) == 5
    assert {s.provider for s in specs.values()} == set(ADAPTERS)
    for spec in specs.values():
        assert "latest" not in spec.api_model
        assert spec.pricing.base.input_per_mtok > 0 and spec.pricing.base.output_per_mtok > 0


@pytest.mark.parametrize(
    ("provider", "api_model"),
    [("anthropic", "claude-opus-latest"), ("google", "gemini-flash-latest"),
     ("openai", "gpt-5.5"), ("openrouter", "lab/open-model:free"),
     ("openrouter", "openrouter/auto")],
)
def test_registry_rejects_floating_aliases(provider, api_model):
    with pytest.raises(RegistryError):
        _spec(provider, api_model=api_model)


def test_registry_requires_openrouter_routing_without_fallbacks():
    with pytest.raises(RegistryError):
        _spec("openrouter", routing={"order": ["somehost"]})


def test_adapter_model_id_is_the_exact_pinned_string():
    for spec in load_registry().values():
        adapter = ADAPTERS[spec.provider](spec, "k", client=httpx.Client())
        assert adapter.model_id == spec.api_model


# -- recording: the ledger lands on the harness's rows, harness untouched ----------


def test_run_recorded_episode_stores_raw_responses_cost_and_retries(tmp_path, monkeypatch):
    # Stand in for Postgres: no reset, an empty snapshot, a canned tool result.
    monkeypatch.setattr(harness, "reset", lambda: None)
    monkeypatch.setattr(harness, "snapshot", lambda: Snapshot(structure={}, digest="d"))
    monkeypatch.setattr(harness, "call_tool",
                        lambda name, args: {"ok": True, "data": {"id": 2}, "error": None})

    adapter, _ = _provider("anthropic", [httpx.Response(529), _json_response(ANTHROPIC_TOOL_TURN),
                                         _json_response(ANTHROPIC_FINAL_TURN)],
                           max_turns=7)
    with Results(tmp_path / "r.db") as results:
        record, usage = run_recorded_episode(
            Task("void-2", "Void invoice 2."), adapter, results, trial_index=0)
        episode = results.episodes_for_run(record.run_id)[0]
        rows = results._conn.execute(
            "SELECT role, raw_response FROM transcripts WHERE episode_id = ? ORDER BY turn_index",
            (record.episode_id,),
        ).fetchall()

    assert record.status == "completed"
    assert episode["model_id"] == "claude-test-1"
    assert episode["retry_count"] == 1
    expected = ((1200 * 2 + 80 * 10) + (1400 * 2 + 5 * 10)) / 1e6
    assert episode["cost_usd"] == pytest.approx(expected) == usage.cost_usd
    assistant_raw = [json.loads(r["raw_response"]) for r in rows if r["role"] == "assistant"]
    assert assistant_raw == [ANTHROPIC_TOOL_TURN, ANTHROPIC_FINAL_TURN]
    assert all(r["raw_response"] is None for r in rows if r["role"] != "assistant")


def test_record_provider_usage_refuses_a_mismatched_ledger(tmp_path):
    with Results(tmp_path / "r.db") as results:
        run_id = results.start_run()
        ep = results.record_episode(run_id, "t", "m", 0, "completed")
        results.record_transcript(ep, [{"turn_index": 0, "role": "assistant", "content": "x"}])
        with pytest.raises(ValueError):
            results.record_provider_usage(ep, cost_usd=0.1, retry_count=0, raw_responses=[])
        assert results.episodes_for_run(run_id)[0]["cost_usd"] is None


# -- opt-in live smoke tests: one trivial, real call per provider ------------------

SMOKE_MODELS = {
    "anthropic": "claude-haiku-4.5",
    "openai": "gpt-5.5",
    "google": "gemini-3.1-pro",
    "openrouter": "qwen3-235b-a22b-2507",
}


@pytest.mark.smoke
@pytest.mark.parametrize("provider", sorted(SMOKE_MODELS))
def test_smoke_live_call(provider):
    spec = load_registry()[SMOKE_MODELS[provider]]
    try:
        adapter = build_provider(spec, retry=RetryPolicy(max_retries=2))
    except RegistryError as exc:
        pytest.skip(str(exc))

    turn = adapter.run_turn(
        [{"role": "user", "content": "Reply with the single word: ok"}], ()
    )

    assert turn.input_tokens > 0 and turn.output_tokens > 0
    assert turn.cost_usd is not None and turn.cost_usd > 0
    payload = json.loads(turn.raw_response)
    # The provider must report serving exactly the pinned version.
    assert adapter.served_model(payload) == spec.api_model
    if provider == "openrouter":
        # OpenRouter reports what it billed: the pinned price must match it.
        assert payload["usage"]["cost"] == pytest.approx(turn.cost_usd, rel=0.02)
