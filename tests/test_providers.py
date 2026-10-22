"""Offline provider tests: tool schema translation and cost math.

No network calls and none of the vendor SDKs need to be installed -- every
adapter module imports its SDK lazily, only inside run_turn(). See the
tests/test_smoke_providers.py for the opt-in @pytest.mark.smoke calls, which
do need credentials and do spend real money.
"""

from __future__ import annotations

import re

import pytest

from consequence.providers import anthropic as anthropic_adapter
from consequence.providers import google as google_adapter
from consequence.providers import openai as openai_adapter
from consequence.providers import openrouter as openrouter_adapter
from consequence.providers.registry import (
    MODELS_YAML_PATH,
    ModelSpec,
    compute_cost,
    load_judge_registry,
    load_model_registry,
    make_provider,
)
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
        # The unchanged JSON Schema, in the field that accepts it whole: Gemini's
        # `parameters` is an OpenAPI subset without additionalProperties.
        assert out["parameters_json_schema"] == tool_def.json_schema()
        assert "parameters" not in out


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


# -- temperature: sent only when set, so the agent runs keep provider defaults ----


def _temperature_in(provider) -> object:
    kwargs = provider._request([{"role": "system", "content": "s"},
                                {"role": "user", "content": "u"}], TOOLS)
    # Anthropic's goes in extra_body: the pinned SDK's create() has no
    # temperature parameter (tests/test_provider_sdk.py calls the real SDK).
    body = kwargs.get("extra_body") or kwargs.get("config", kwargs)
    return body.get("temperature", "absent")


@pytest.mark.parametrize("make", [
    lambda **kw: anthropic_adapter.AnthropicProvider(model_id="m", api_model="a", **kw),
    lambda **kw: openai_adapter.OpenAIProvider(model_id="m", api_model="a", **kw),
    lambda **kw: google_adapter.GoogleProvider(model_id="m", api_model="a", **kw),
    lambda **kw: openrouter_adapter.OpenRouterProvider(model_id="m", api_model="a", **kw),
], ids=["anthropic", "openai", "google", "openrouter"])
def test_temperature_is_sent_only_when_set(make):
    assert _temperature_in(make()) == "absent"  # agent runs: provider default
    assert _temperature_in(make(temperature=0.0)) == 0.0  # 0 is a value, not "unset"
    assert _temperature_in(make(temperature=0.7)) == 0.7


# -- credentials: each adapter sends its own vendor's key -----------------------------


def test_openrouter_never_sends_the_openai_key(monkeypatch):
    """The openai SDK reads OPENAI_API_KEY when given no key; OpenRouter must
    use OPENROUTER_API_KEY or refuse, never hand an OpenAI secret to a third
    party."""
    provider = openrouter_adapter.OpenRouterProvider(model_id="m", api_model="a/b")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="never falls back to OPENAI_API_KEY"):
        provider._resolved_api_key()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
    assert provider._resolved_api_key() == "sk-or-key"
    explicit = openrouter_adapter.OpenRouterProvider(model_id="m", api_model="a/b", api_key="k")
    assert explicit._resolved_api_key() == "k"


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


def floating_reason(provider: str, api_model: str) -> str | None:
    """Why `api_model` is a moving alias rather than an immutable version, per
    each vendor's documentation; None if it is pinned.

    - Anthropic: "Every Claude model ID is a pinned snapshot, including the
      dateless IDs used from the 4.6 generation on"; before that, the dateless
      ID "is a convenience pointer that resolves to the dated ID"
      (platform.claude.com/docs/en/about-claude/models/overview). So a dateless
      ID is pinned only from 4.6 on; older ones need their -YYYYMMDD snapshot.
    - OpenAI: snapshots "let you lock in a specific version of the model" and
      are the -YYYY-MM-DD form; the bare family name is not one.
    - Google: "Stable: Points to a specific stable model. Stable models
      usually don't change", unlike preview and experimental codes
      (ai.google.dev/gemini-api/docs/models). So stable codes only.
    - Every vendor: anything containing "latest" is hot-swapped by definition.
    """
    if not api_model:
        return "empty"
    if "latest" in api_model.lower():
        return "a 'latest' alias"
    if provider == "anthropic" and not re.search(r"-\d{8}$", api_model):
        version = re.match(r"claude-[a-z]+-(\d+)(?:-(\d+))?$", api_model)
        if version is None:
            return "not a recognised Claude model ID"
        if (int(version.group(1)), int(version.group(2) or 0)) < (4, 6):
            return "a dateless Claude ID before the 4.6 generation is an alias"
    if provider == "openai" and not re.search(r"-\d{4}-\d{2}-\d{2}$", api_model):
        return "not an OpenAI dated snapshot"
    if provider == "google" and re.search(r"-(preview|exp)\b|-exp-|preview", api_model):
        return "a Google preview or experimental code, not a stable one"
    return None


@pytest.mark.parametrize("provider, api_model, floating", [
    ("anthropic", "claude-haiku-4-5", True),  # an alias for claude-haiku-4-5-20251001
    ("anthropic", "claude-haiku-4-5-20251001", False),
    ("anthropic", "claude-opus-5", False),  # dateless, but from 4.6 on: pinned
    ("anthropic", "claude-opus-4-6", False),
    ("anthropic", "claude-3-5-sonnet-latest", True),
    ("openai", "gpt-5.1", True),
    ("openai", "gpt-5.1-2025-11-13", False),
    ("google", "gemini-2.5-flash-latest", True),
    ("google", "gemini-3.1-pro-preview", True),
    ("google", "gemini-4-argon", False),
    ("google", "gemini-3.5-flash-lite", False),
])
def test_the_floating_alias_rule_itself(provider, api_model, floating):
    assert (floating_reason(provider, api_model) is not None) == floating


def test_models_yaml_loads_five_models_with_pinned_non_floating_versions():
    """Every api_model, in models: and judges:, is an immutable version."""
    registry = load_model_registry()
    assert len(registry) == 5
    providers = {spec.provider for spec in registry.values()}
    assert providers == {"anthropic", "openai", "google", "openrouter"}
    judges = load_judge_registry()
    assert judges
    for spec in [*registry.values(), *judges.values()]:
        assert floating_reason(spec.provider, spec.api_model) is None, (
            spec.id, spec.api_model, floating_reason(spec.provider, spec.api_model))
        assert spec.input_price_per_million > 0
        assert spec.output_price_per_million > 0


def test_every_judge_has_a_documented_context_window():
    """Set from the vendor's page for that api_model, which models.yaml cites
    beside the value; the judge refuses to run without one."""
    text = MODELS_YAML_PATH.read_text(encoding="utf-8")
    for spec in load_judge_registry().values():
        assert isinstance(spec.context_window_tokens, int) and spec.context_window_tokens > 0
        block = text[text.index(f"id: {spec.id}"):]
        assert "context_window_tokens" in block and "https://" in block.split(
            "context_window_tokens")[0], spec.id  # a URL precedes the value


# -- Anthropic: structured-output verdicts and thinking display -----------------------------


def test_structured_output_schema_is_one_the_api_accepts():
    """Structured outputs support no numeric bounds and need closed objects:
    the bounds move into the description (parse_vote still enforces them)."""
    from consequence.graders.judge_prompt import VERDICT_TOOL

    schema = anthropic_adapter.output_schema(VERDICT_TOOL)
    confidence = schema["properties"]["confidence"]
    assert "minimum" not in confidence and "maximum" not in confidence
    assert "minimum 0" in confidence["description"] and "maximum 1" in confidence["description"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["passed", "confidence", "reasoning", "behaviour"]
    assert VERDICT_TOOL.json_schema()["properties"]["confidence"]["minimum"] == 0  # untouched


def test_structured_output_sends_a_schema_instead_of_tools():
    from consequence.graders.judge_prompt import VERDICT_TOOL

    provider = anthropic_adapter.AnthropicProvider(model_id="m", api_model="a",
                                                   structured_output=True)
    kwargs = provider._request([{"role": "user", "content": "u"}], (VERDICT_TOOL,))
    assert "tools" not in kwargs and "tool_choice" not in kwargs
    assert kwargs["output_config"]["format"]["schema"] == anthropic_adapter.output_schema(
        VERDICT_TOOL)
    with pytest.raises(ValueError, match="exactly one tool"):
        provider._request([{"role": "user", "content": "u"}], TOOLS)


def test_thinking_display_is_validated_and_updates_sends_its_beta_header():
    with pytest.raises(ValueError, match="thinking_display"):
        anthropic_adapter.AnthropicProvider(model_id="m", api_model="a", thinking_display="all")
    plain = anthropic_adapter.AnthropicProvider(model_id="m", api_model="a")._request(
        [{"role": "user", "content": "u"}], TOOLS)
    assert "thinking" not in plain and "extra_headers" not in plain
    updates = anthropic_adapter.AnthropicProvider(
        model_id="m", api_model="a", thinking_display="updates")._request(
        [{"role": "user", "content": "u"}], TOOLS)
    assert updates["thinking"] == {"type": "adaptive", "display": "updates"}
    assert updates["extra_headers"] == {"anthropic-beta": anthropic_adapter.DISPLAY_UPDATES_BETA}


def test_progress_updates_are_narration_only_when_display_returns_them():
    from types import SimpleNamespace

    blocks = [SimpleNamespace(type="thinking", thinking=""),
              SimpleNamespace(type="thinking", thinking="Checking invoice 12 next."),
              SimpleNamespace(type="tool_use", id="t1", name="get_invoice",
                              input={"invoice_id": 12}),
              SimpleNamespace(type="text", text="Invoice 12 "),
              SimpleNamespace(type="text", text="is active.")]
    response = SimpleNamespace(content=blocks, stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                               to_dict=lambda: {"model": "claude-opus-5-5", "content": []})
    shown = anthropic_adapter.parse_response(response, 0, 0, progress_updates=True)
    hidden = anthropic_adapter.parse_response(response, 0, 0)
    assert shown.text == "Checking invoice 12 next.\n\nInvoice 12 is active."
    assert hidden.text == "Invoice 12 is active."  # as before: text blocks only


def test_make_provider_applies_the_entry_s_options_and_output_cap():
    registry = load_model_registry()
    opus = make_provider("claude-opus-5.5", registry=registry)
    assert opus.thinking_display == "updates"
    gemini = make_provider("gemini-4-argon", registry=registry, max_output_tokens=777)
    assert gemini.max_output_tokens == 777  # Google's own name for the cap
    gpt = make_provider("gpt-5.1", registry=registry, max_output_tokens=555)
    assert gpt.max_tokens == 555
