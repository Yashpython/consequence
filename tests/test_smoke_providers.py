"""Opt-in, billed smoke tests: one minimal real call per judge condition and
per evaluated model, to prove credentials, pinned model IDs, parameters and
tool calling all work before the real run. Never run in CI.

    pip install -e ".[dev,providers]"   # the vendor SDKs
    pytest -m smoke -rf                 # needs every *_API_KEY in .env

Each call is capped at SMOKE_MAX_TOKENS. Nothing here is a measurement: these
check wiring, not quality.
"""

from __future__ import annotations

import json

import pytest

from consequence.graders.judge import STABILITY_TEMPERATURE, build_messages, parse_vote
from consequence.graders.judge_prompt import BEHAVIOURS, VERDICT_TOOL
from consequence.harness import SYSTEM_PROMPT
from consequence.providers.registry import (
    ModelSpec,
    load_judge_registry,
    load_model_registry,
    make_provider,
)
from consequence.tools.schema import TOOLS

JUDGES = load_judge_registry()
MODELS = load_model_registry()
# Room for a short verdict or one tool call, including any reasoning tokens.
SMOKE_MAX_TOKENS = 2048

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


def _limit(spec: ModelSpec) -> dict[str, int]:
    """Each adapter's own name for the output cap."""
    key = "max_output_tokens" if spec.provider == "google" else "max_tokens"
    return {key: SMOKE_MAX_TOKENS}


@pytest.mark.smoke
@pytest.mark.parametrize("temperature", [0.0, STABILITY_TEMPERATURE], ids=["t0", "t0.7"])
@pytest.mark.parametrize("judge_id", sorted(JUDGES))
def test_judge_returns_a_parseable_verdict(judge_id, temperature):
    """The judge accepts this temperature and answers through the forced
    record_verdict call with a verdict parse_vote accepts."""
    spec = JUDGES[judge_id]
    provider = make_provider(judge_id, registry=JUDGES, temperature=temperature, **_limit(spec))
    turn = provider.run_turn(build_messages(TINY_TRANSCRIPT), (VERDICT_TOOL,))
    vote = parse_vote(turn)  # raises MalformedVerdict if it isn't one valid call
    assert vote.behaviour in BEHAVIOURS
    assert turn.input_tokens > 0 and turn.output_tokens > 0  # usage is reported, for cost


@pytest.mark.smoke
@pytest.mark.parametrize("model_id", sorted(MODELS))
def test_evaluated_model_makes_a_parseable_tool_call(model_id):
    """One real agent turn with the benchmark's tools attached: the model
    answers with a tool call the harness can execute."""
    provider = make_provider(model_id, registry=MODELS, **_limit(MODELS[model_id]))
    turn = provider.run_turn(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": "Look up invoice 12 with the get_invoice tool."}],
        TOOLS,
    )
    assert turn.tool_calls, f"no tool call; text was {turn.text!r}"
    call = turn.tool_calls[0]
    assert call.name in {t.name for t in TOOLS}
    assert isinstance(call.arguments, dict)
    assert turn.input_tokens > 0 and turn.output_tokens > 0
