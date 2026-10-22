"""Argument enforcement is the tool layer's, identical for every model.

Each vendor receives the tool schemas in its own wire shape (Gemini as
parameters_json_schema, the others as their tool format), and a vendor may or
may not hold a model to `additionalProperties: false` when it generates a
call. That is never what the benchmark relies on: every call, whichever
adapter parsed it, goes through consequence.tools.impl._validate_args, which
refuses any argument the tool doesn't declare. So no model can pass a field
the others can't.

These tests run the real harness offline (no Postgres): a write call carrying
an undeclared argument, parsed from each vendor's own response shape, must be
refused with the same error and leave the database unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from consequence.harness import run_episode
from consequence.providers import anthropic as anthropic_adapter
from consequence.providers import google as google_adapter
from consequence.providers import openai as openai_adapter
from consequence.providers.base import TurnResult
from consequence.results import Results
from consequence.tasks import TASKS_BY_ID
from consequence.tools.schema import TOOLS_BY_NAME

# A real write the task allows, plus a field void_invoice doesn't declare.
SMUGGLED = {"invoice_id": 12, "reason": "duplicate of invoice 1", "force": True}
REFUSAL = "unknown argument(s): force"


def _anthropic():
    block = SimpleNamespace(type="tool_use", id="tu_1", name="void_invoice", input=SMUGGLED)
    return anthropic_adapter.parse_response(SimpleNamespace(
        content=[block], stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        to_dict=lambda: {"model": "claude-opus-5-5", "content": []}), 0, 0)


def _openai():
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
        name="void_invoice", arguments=json.dumps(SMUGGLED)))
    message = SimpleNamespace(content=None, tool_calls=[call])
    return openai_adapter.parse_response(SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        to_dict=lambda: {"model": "gpt-5.1-2025-11-13"}), 0, 0)


def _google():
    part = SimpleNamespace(text=None, function_call=SimpleNamespace(name="void_invoice",
                                                                    args=SMUGGLED))
    return google_adapter.parse_response(SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]),
                                    finish_reason=SimpleNamespace(name="STOP"))],
        model_dump=lambda mode=None: {"model_version": "gemini-4-argon"},
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                                       thoughts_token_count=0)), 0, 0)


class Scripted:
    """Replays one parsed vendor turn, then ends the episode."""

    model_id = "scripted"

    def __init__(self, first: TurnResult):
        self.turns = [first, TurnResult(text="Done.")]

    def run_turn(self, messages, tools):
        return self.turns.pop(0)


# OpenRouter parses with the OpenAI adapter's parse_response.
@pytest.mark.parametrize("parse", [_anthropic, _openai, _google],
                         ids=["anthropic", "openai-and-openrouter", "google"])
def test_an_undeclared_argument_is_refused_by_the_tool_layer_for_every_vendor(
        offline_harness, tmp_path, parse):
    turn = parse()
    assert turn.tool_calls[0].arguments == SMUGGLED  # the adapter passed it through as sent
    with Results(tmp_path / "r.db") as results:
        record = run_episode(TASKS_BY_ID["001_void_duplicate"], Scripted(turn), results, 0)
        [tool_turn] = [t for t in results.transcript_for_episode(record.episode_id)
                       if t["role"] == "tool"]
        stored = results.state_diff_for_episode(record.episode_id)
    result = json.loads(tool_turn["tool_result"])
    assert (result["ok"], result["error"]) == (False, REFUSAL)
    assert stored["digest_before"] == stored["digest_after"]  # nothing was written


def test_every_tool_schema_declares_its_arguments_closed():
    """The schema each vendor receives says the same thing the tool layer
    enforces: no argument outside the declared ones."""
    for name, tool in TOOLS_BY_NAME.items():
        assert tool.json_schema()["additionalProperties"] is False, name
