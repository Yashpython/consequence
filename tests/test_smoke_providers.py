"""Opt-in, billed smoke tests: one minimal real call per judge condition and
per evaluated model, to prove credentials, pinned model IDs, parameters and
tool calling all work before the real run. Never run in CI.

    pip install -e ".[dev,providers]"   # the vendor SDKs
    pytest -m smoke -rf                 # needs every *_API_KEY in .env

Every provider is built exactly as the real calls build it: judges as
`consequence grade` builds them (make_provider with the judge's temperature,
or none for a judge at the provider's default sampling), models as
`consequence run` does (make_provider(model_id)). So the output caps, the
thinking display setting and the structured-output verdict are the ones the
experiment uses. tests/test_provider_sdk.py runs these same calls offline,
through the real SDKs against a fake transport, so a request an SDK would
refuse fails there first, for free. Nothing here is a measurement: these
check wiring, not quality.
"""

from __future__ import annotations

import json
import os

import pytest

from consequence.graders.judge import STABILITY_TEMPERATURE, build_messages, parse_vote
from consequence.graders.judge_prompt import BEHAVIOURS, VERDICT_TOOL
from consequence.harness import SYSTEM_PROMPT  # imports consequence.config, which loads .env
from consequence.providers.registry import (
    PROVIDER_DEFAULT,
    ModelSpec,
    load_judge_registry,
    load_model_registry,
    make_provider,
)
from consequence.tools.schema import TOOLS

JUDGES = load_judge_registry()
MODELS = load_model_registry()
API_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}

# A complete, tiny transcript of the shape the harness stores: tool arguments
# and results as JSON text.
TINY_TRANSCRIPT = [
    {"turn_index": 0, "role": "system", "content": SYSTEM_PROMPT, "tool_name": None,
     "tool_args": None, "tool_result": None},
    {"turn_index": 1, "role": "user", "content": "Invoice 12 duplicates invoice 1. Void 12.",
     "tool_name": None, "tool_args": None, "tool_result": None},
    {"turn_index": 2, "role": "assistant", "content": None, "tool_name": None,
     "tool_args": None, "tool_result": None},
    {"turn_index": 3, "role": "tool", "content": None, "tool_name": "void_invoice",
     "tool_args": json.dumps({"invoice_id": 12, "reason": "duplicate of invoice 1"}),
     "tool_result": json.dumps({"ok": True, "data": {"invoice_id": 12, "status": "void"},
                                "error": None})},
    {"turn_index": 4, "role": "assistant", "content": "Voided invoice 12.", "tool_name": None,
     "tool_args": None, "tool_result": None},
]
# A first turn that reasonably elicits reasoning before the tool call: the
# model has to plan how to check a possible duplicate, not just echo a lookup.
# Adaptive thinking can still skip reasoning on a request it finds easy; then
# the round trip is unproven and the test skips (round_trip_unproven).
AGENT_TASK = ("Invoice 12 might be a duplicate of another invoice from the same supplier, "
              "or it might not. Before acting, think through what would distinguish a true "
              "duplicate from a legitimate repeat order, then start by looking up invoice 12 "
              "with the get_invoice tool.")
# The adapters whose replay carries reasoning state the vendor requires back
# verbatim: Claude thinking blocks, Gemini thought signatures. OpenAI-wire
# Chat Completions (OpenAI, OpenRouter) return none to replay.
REASONING_REPLAY_PROVIDERS = frozenset({"anthropic", "google"})
UNPROVEN = "no reasoning produced; round trip unproven"
TOOL_RESULT = {"ok": True, "error": None,
               "data": {"id": 12, "number": "INV-01-012", "status": "active", "total": "120.00"}}


def judge_conditions() -> list[tuple[str, float | None]]:
    """(judge id, temperature) for every condition a judge is run under: 0
    and the stability temperature for a judge that takes one, the provider's
    default (None) for one that rejects it."""
    out: list[tuple[str, float | None]] = []
    for judge_id in sorted(JUDGES):
        if JUDGES[judge_id].sampling == PROVIDER_DEFAULT:
            out.append((judge_id, None))
        else:
            out += [(judge_id, 0.0), (judge_id, STABILITY_TEMPERATURE)]
    return out


def _condition_id(condition: tuple[str, float | None]) -> str:
    judge_id, temperature = condition
    return f"{judge_id}-{'provider_default' if temperature is None else f't{temperature:g}'}"


def build_judge(judge_id: str, temperature: float | None):
    """As `consequence grade` builds it (cli._judge_setup)."""
    return make_provider(judge_id, registry=JUDGES, temperature=temperature)


def build_model(model_id: str):
    """As `consequence run` builds it (cli.cmd_run)."""
    return make_provider(model_id, registry=MODELS)


def agent_messages() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": AGENT_TASK}]


def with_tool_result(messages: list[dict], turn) -> list[dict]:
    """The conversation after `turn`, as the harness extends it."""
    out = [*messages, {"role": "assistant", "content": turn.text, "replay": turn.replay,
                       "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                                      for c in turn.tool_calls]}]
    out += [{"role": "tool", "tool_call_id": c.id, "name": c.name, "content": TOOL_RESULT}
            for c in turn.tool_calls]
    return out


def round_trip_unproven(spec: ModelSpec, first) -> bool:
    """For a model whose adapter replays reasoning state, whether the first
    reply carried none: then the second call proved nothing about the round
    trip, and the test must skip, never pass."""
    return spec.provider in REASONING_REPLAY_PROVIDERS and not first.reasoning_state


def _require_key(spec: ModelSpec) -> None:
    name = API_KEYS.get(spec.provider)
    if name and not os.environ.get(name):
        pytest.fail(f"{name} is not set (in .env or the environment); {spec.id} needs it")


def _call(spec: ModelSpec, run, *, temperature: float | None = None):
    """Make the call; on failure, say what to change, not just what broke."""
    try:
        return run()
    except ImportError as exc:
        pytest.fail(f"{spec.id}: vendor SDK missing ({exc}); run "
                    "pip install -e \".[dev,providers]\"")
    except Exception as exc:  # noqa: BLE001 -- re-raised as a failure with a hint
        hint = ""
        if temperature is not None and "temperature" in str(exc).lower():
            hint = (f"\nThe API refused temperature {temperature}. If this judge rejects any "
                    f"temperature, set `sampling: provider_default` for {spec.id} in "
                    "consequence/models.yaml (docs/judge-methodology.md).")
        pytest.fail(f"{spec.id}: {type(exc).__name__}: {exc}{hint}")


@pytest.mark.smoke
@pytest.mark.parametrize("condition", judge_conditions(), ids=_condition_id)
def test_judge_returns_a_parseable_verdict(condition):
    """The judge accepts this sampling condition and returns a verdict
    parse_vote accepts: a record_verdict call, or for a judge that answers
    through structured output, the JSON its reply is constrained to."""
    judge_id, temperature = condition
    spec = JUDGES[judge_id]
    _require_key(spec)
    provider = build_judge(judge_id, temperature)
    turn = _call(spec, lambda: provider.run_turn(build_messages(TINY_TRANSCRIPT),
                                                 (VERDICT_TOOL,)), temperature=temperature)
    vote = parse_vote(turn)  # raises MalformedVerdict if it isn't one valid verdict
    assert vote.behaviour in BEHAVIOURS
    assert turn.input_tokens > 0 and turn.output_tokens > 0  # usage is reported, for cost
    assert turn.served_model  # provenance: the response names what served it


@pytest.mark.smoke
@pytest.mark.parametrize("model_id", sorted(MODELS))
def test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result(model_id):
    """Two real agent turns with the benchmark's tools attached, exactly as
    the harness sends them: a tool call the harness can execute, then the
    tool result and one more call.

    For Claude Opus 5.5 and the Gemini models, the first reply must carry
    reasoning state (thinking blocks, thought signatures) and the second call,
    carrying it back verbatim, must be accepted: a pass means the round trip
    was exercised. If the first reply carried none, the test SKIPS with
    UNPROVEN, never passes."""
    spec = MODELS[model_id]
    _require_key(spec)
    provider = build_model(model_id)
    messages = agent_messages()
    turn = _call(spec, lambda: provider.run_turn(messages, TOOLS))
    assert turn.stop_reason not in ("max_tokens", "length"), (
        "the output cap ran out before a tool call")
    assert turn.tool_calls, f"no tool call; text was {turn.text!r}"
    call = turn.tool_calls[0]
    assert call.name in {t.name for t in TOOLS}
    assert isinstance(call.arguments, dict)
    assert turn.input_tokens > 0 and turn.output_tokens > 0
    assert turn.served_model  # provenance: the response names what served it

    second = _call(spec, lambda: provider.run_turn(with_tool_result(messages, turn), TOOLS))
    assert second.text or second.tool_calls  # the conversation was accepted and continued
    if round_trip_unproven(spec, turn):
        pytest.skip(UNPROVEN)
