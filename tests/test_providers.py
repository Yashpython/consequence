"""Offline provider tests: tool schema translation and cost math.

No network calls and none of the vendor SDKs need to be installed -- every
adapter module imports its SDK lazily, only inside run_turn(). See the
bottom of this file for the one opt-in @pytest.mark.smoke test per
provider, which does need credentials and does spend real money.
"""

from __future__ import annotations

import pytest

from consequence.providers import anthropic as anthropic_adapter
from consequence.providers import google as google_adapter
from consequence.providers import openai as openai_adapter
from consequence.providers import openrouter as openrouter_adapter
from consequence.providers.registry import ModelSpec, compute_cost, load_model_registry
from consequence.tools.schema import TOOLS

TOOL_NAMES = {t.name for t in TOOLS}


# -- tool schema translation, per provider, against a fixture -------------


def test_anthropic_tool_translation_matches_schema():
    translated = anthropic_adapter.translate_tools(TOOLS)
    assert {t["name"] for t in translated} == TOOL_NAMES
    for tool_def, out in zip(TOOLS, translated, strict=True):
        assert out["name"] == tool_def.name
        assert out["description"] == tool_def.description
        assert out["input_schema"] == tool_def.json_schema()


def test_openai_tool_translation_matches_schema():
    translated = openai_adapter.translate_tools(TOOLS)
    assert {t["function"]["name"] for t in translated} == TOOL_NAMES
    for tool_def, out in zip(TOOLS, translated, strict=True):
        assert out["type"] == "function"
        assert out["function"]["name"] == tool_def.name
        assert out["function"]["description"] == tool_def.description
        assert out["function"]["parameters"] == tool_def.json_schema()


def test_google_tool_translation_matches_schema():
    [translated] = google_adapter.translate_tools(TOOLS)
    declarations = translated["function_declarations"]
    assert {d["name"] for d in declarations} == TOOL_NAMES
    for tool_def, out in zip(TOOLS, declarations, strict=True):
        assert out["name"] == tool_def.name
        assert out["description"] == tool_def.description
        assert out["parameters"] == tool_def.json_schema()


def test_openrouter_tool_translation_matches_openai_shape():
    # OpenRouter is OpenAI-wire-compatible; it must translate identically.
    assert openrouter_adapter.translate_tools(TOOLS) == openai_adapter.translate_tools(TOOLS)


# -- message translation round trips ---------------------------------------


def _sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "void invoice 2"},
        {
            "role": "assistant",
            "content": "voiding it",
            "tool_calls": [
                {"id": "call_1", "name": "void_invoice", "arguments": {"invoice_id": 2, "reason": "x"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "void_invoice", "content": {"ok": True}},
    ]


def test_anthropic_message_translation_extracts_system_and_tool_blocks():
    system, messages = anthropic_adapter.translate_messages(_sample_messages())
    assert system == "be terse"
    assert messages[0] == {"role": "user", "content": "void invoice 2"}
    assert messages[1]["content"][0] == {"type": "text", "text": "voiding it"}
    assert messages[1]["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "void_invoice",
        "input": {"invoice_id": 2, "reason": "x"},
    }
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert messages[2]["content"][0]["tool_use_id"] == "call_1"


def test_openai_message_translation_keeps_tool_role():
    messages = openai_adapter.translate_messages(_sample_messages())
    assert messages[0] == {"role": "system", "content": "be terse"}
    assert messages[2]["tool_calls"][0]["function"]["name"] == "void_invoice"
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true}',
    }


def test_google_message_translation_uses_model_role_and_function_response():
    system, contents = google_adapter.translate_messages(_sample_messages())
    assert system == "be terse"
    assert contents[0] == {"role": "user", "parts": [{"text": "void invoice 2"}]}
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][1]["function_call"] == {
        "name": "void_invoice",
        "args": {"invoice_id": 2, "reason": "x"},
    }
    assert contents[2]["parts"][0]["function_response"] == {
        "name": "void_invoice",
        "response": {"ok": True},
    }


def test_unknown_message_role_raises_in_every_adapter():
    bad = [{"role": "narrator", "content": "???"}]
    for translate in (
        lambda m: anthropic_adapter.translate_messages(m),
        openai_adapter.translate_messages,
        google_adapter.translate_messages,
    ):
        with pytest.raises(ValueError, match="unknown message role"):
            translate(bad)


# -- cost calculation, unit-tested against known token counts and prices --


def test_compute_cost_known_token_counts_and_prices():
    registry = {
        "test-model": ModelSpec(
            id="test-model",
            provider="anthropic",
            api_model="test-model-v1",
            input_price_per_million=5.0,
            output_price_per_million=25.0,
        )
    }
    # 1,000,000 input tokens @ $5/M + 200,000 output tokens @ $25/M
    cost = compute_cost("test-model", 1_000_000, 200_000, registry=registry)
    assert cost == pytest.approx(5.0 + 5.0)


def test_compute_cost_zero_tokens_is_zero():
    registry = {
        "m": ModelSpec(id="m", provider="openai", api_model="m-v1",
                        input_price_per_million=1.25, output_price_per_million=10.0)
    }
    assert compute_cost("m", 0, 0, registry=registry) == 0.0


def test_compute_cost_unknown_model_is_none_not_a_guess():
    assert compute_cost("does-not-exist", 100, 100, registry={}) is None


def test_models_yaml_loads_five_models_with_pinned_non_floating_versions():
    registry = load_model_registry()
    assert len(registry) == 5
    providers = {spec.provider for spec in registry.values()}
    assert providers == {"anthropic", "openai", "google", "openrouter"}
    for spec in registry.values():
        assert spec.api_model  # non-empty
        assert spec.api_model not in ("latest", "-latest")
        assert "latest" not in spec.api_model.lower()
        assert spec.input_price_per_million > 0
        assert spec.output_price_per_million > 0


# -- one opt-in, credentialed, real smoke test per provider ---------------
# Skipped by default (pytest addopts excludes "smoke"); run explicitly with
# `pytest -m smoke`. Each makes exactly one trivial, billed API call with no
# tools, just to confirm credentials and wiring work end to end.


@pytest.mark.smoke
def test_anthropic_smoke():
    from consequence.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(model_id="claude-haiku-4.5", api_model="claude-haiku-4-5")
    result = provider.run_turn([{"role": "user", "content": "Say 'ok'."}], tools=[])
    assert result.text


@pytest.mark.smoke
def test_openai_smoke():
    from consequence.providers.openai import OpenAIProvider

    provider = OpenAIProvider(model_id="gpt-5.1", api_model="gpt-5.1-2025-11-13")
    result = provider.run_turn([{"role": "user", "content": "Say 'ok'."}], tools=[])
    assert result.text


@pytest.mark.smoke
def test_google_smoke():
    from consequence.providers.google import GoogleProvider

    provider = GoogleProvider(model_id="gemini-3.1-pro", api_model="gemini-3.1-pro-preview")
    result = provider.run_turn([{"role": "user", "content": "Say 'ok'."}], tools=[])
    assert result.text


@pytest.mark.smoke
def test_openrouter_smoke():
    from consequence.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(model_id="deepseek-v3.2", api_model="deepseek/deepseek-v3.2")
    result = provider.run_turn([{"role": "user", "content": "Say 'ok'."}], tools=[])
    assert result.text
