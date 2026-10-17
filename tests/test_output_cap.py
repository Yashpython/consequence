"""The agent output cap: one cap for every model under test, every agent turn
records why it ended, and episodes with a turn cut off by the cap are counted
per model.

A reasoning model that spends a small cap on thinking returns no tool call
and is scored as failing, for a reason unrelated to its behaviour. The cap is
uniform and well above normal use; output_capped_episodes measures the
truncation that remains instead of assuming there is none.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import yaml
from helpers import seed_run

from consequence.harness import run_episode
from consequence.metrics import RunMetrics
from consequence.providers import anthropic as anthropic_adapter
from consequence.providers import google as google_adapter
from consequence.providers import openai as openai_adapter
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.mock import MockProvider
from consequence.providers.registry import (
    MODELS_YAML_PATH,
    load_judge_registry,
    load_model_registry,
    make_provider,
)
from consequence.report import build_report, render_markdown
from consequence.results import Results
from consequence.tasks import TASKS_BY_ID

AGENT_CAP = 16_000
ANTHROPIC_SDK_NONSTREAMING_LIMIT = 21_333  # 3600 s x max_tokens / 128,000 <= 600 s

# -- one cap, the same for every model under test --------------------------------------------


def test_every_model_under_test_runs_at_the_same_output_cap():
    models = load_model_registry()
    assert {spec.max_output_tokens for spec in models.values()} == {AGENT_CAP}
    assert AGENT_CAP < ANTHROPIC_SDK_NONSTREAMING_LIMIT
    for model_id in models:
        provider = make_provider(model_id, registry=models)  # as `consequence run` builds it
        cap = getattr(provider, "max_output_tokens", None) or provider.max_tokens
        assert cap == AGENT_CAP, model_id


def test_a_per_model_cap_or_a_missing_cap_is_refused(tmp_path):
    raw = yaml.safe_load(MODELS_YAML_PATH.read_text(encoding="utf-8"))
    per_model = {**raw, "models": [{**raw["models"][0], "max_output_tokens": 4096},
                                   *raw["models"][1:]]}
    (tmp_path / "per_model.yaml").write_text(yaml.safe_dump(per_model))
    with pytest.raises(ValueError, match="no per-model max_output_tokens"):
        load_model_registry(tmp_path / "per_model.yaml")
    missing = {k: v for k, v in raw.items() if k != "agent_max_output_tokens"}
    (tmp_path / "missing.yaml").write_text(yaml.safe_dump(missing))
    with pytest.raises(ValueError, match="agent_max_output_tokens"):
        load_model_registry(tmp_path / "missing.yaml")


def test_judges_keep_their_own_caps():
    judges = load_judge_registry()
    assert judges["judge-claude-opus-5.5"].max_output_tokens == 16_000
    assert judges["judge-gpt-5.1"].max_output_tokens is None  # the adapter default


# -- each adapter maps its vendor's cap stop to output_capped --------------------------------


def _anthropic(stop: str):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="")],
                           stop_reason=stop, usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                           to_dict=lambda: {"model": "claude-opus-5-5", "content": []})


def _openai(finish: str):
    message = SimpleNamespace(content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                           to_dict=lambda: {"model": "gpt-5.1-2025-11-13"})


def _google(finish):
    content = SimpleNamespace(parts=[])
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content, finish_reason=finish)],
        model_dump=lambda mode=None: {"model_version": "gemini-4-argon"},
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=0,
                                       thoughts_token_count=16_000))


@pytest.mark.parametrize("response, parse, stop, capped", [
    (_anthropic("max_tokens"), anthropic_adapter.parse_response, "max_tokens", True),
    (_anthropic("end_turn"), anthropic_adapter.parse_response, "end_turn", False),
    (_openai("length"), openai_adapter.parse_response, "length", True),
    (_openai("stop"), openai_adapter.parse_response, "stop", False),
    # Gemini's own finish reason, verbatim: it used to be replaced by
    # "end_turn", which hid a turn that spent the whole cap on thinking.
    (_google(SimpleNamespace(name="MAX_TOKENS")), google_adapter.parse_response, "MAX_TOKENS",
     True),
    (_google(SimpleNamespace(name="STOP")), google_adapter.parse_response, "STOP", False),
    (_google(None), google_adapter.parse_response, "end_turn", False),
], ids=["anthropic-cap", "anthropic-end", "openai-cap", "openai-stop", "google-cap",
        "google-stop", "google-none"])
def test_each_adapter_flags_a_turn_cut_off_by_the_cap(response, parse, stop, capped):
    turn = parse(response, 0, 0)
    assert (turn.stop_reason, turn.output_capped) == (stop, capped)


# -- stored on every agent turn --------------------------------------------------------------


def test_every_agent_turn_stores_why_it_ended(offline_harness, tmp_path):
    """The cut-off turn carries no tool call, so the episode ends there and is
    scored as it stands; the transcript says why."""
    provider = MockProvider(model_id="mock", script=[
        TurnResult(text=None, tool_calls=(ToolCall("c0", "get_invoice", {"invoice_id": 12}),),
                   stop_reason="tool_use"),
        TurnResult(text=None, stop_reason="max_tokens", output_capped=True),
    ])
    with Results(tmp_path / "r.db") as results:
        record = run_episode(TASKS_BY_ID["001_void_duplicate"], provider, results, 0)
        turns = results.transcript_for_episode(record.episode_id)
    agent = [(t["stop_reason"], t["output_capped"]) for t in turns if t["role"] == "assistant"]
    assert agent == [("tool_use", 0), ("max_tokens", 1)]
    assert all(t["stop_reason"] is None and t["output_capped"] is None
               for t in turns if t["role"] != "assistant")


@pytest.mark.parametrize("turn", [
    {"turn_index": 0, "role": "assistant", "content": "x"},  # neither
    {"turn_index": 0, "role": "assistant", "content": "x", "stop_reason": "end_turn"},
    {"turn_index": 0, "role": "assistant", "content": "x", "output_capped": False},
    {"turn_index": 0, "role": "user", "content": "x", "stop_reason": "end_turn",
     "output_capped": False},
])
def test_the_schema_requires_the_stop_fields_on_agent_turns_only(tmp_path, turn):
    with Results(tmp_path / "r.db") as results:
        run_id = seed_run(results)
        episode = results.record_episode(run_id=run_id, task_id="001_void_duplicate",
                                         model_id="m", trial_index=0, status="completed")
        with pytest.raises(sqlite3.IntegrityError):
            results.record_transcript(episode, [turn])


# -- measured per model and reported ---------------------------------------------------------


def _episode(results, run_id, task_id, model, capped_turns):
    episode = results.record_episode(run_id=run_id, task_id=task_id, model_id=model,
                                     trial_index=0, status="completed")
    results.record_transcript(episode, [
        {"turn_index": i, "role": "assistant", "content": None,
         "stop_reason": "max_tokens" if capped else "end_turn", "output_capped": capped}
        for i, capped in enumerate(capped_turns)])
    return episode


def test_output_capped_episodes_counts_episodes_not_turns_per_model_and_category(tmp_path):
    path = tmp_path / "r.db"
    with Results(path) as results:
        run_id = seed_run(results)
        _episode(results, run_id, "001_void_duplicate", "m-a", [False, True, True])
        _episode(results, run_id, "001_void_duplicate", "m-a", [False])
        _episode(results, run_id, "001_void_duplicate", "m-a", [])  # no agent turn at all
        _episode(results, run_id, "013_void_paid_invoice", "m-a", [True])
        _episode(results, run_id, "001_void_duplicate", "m-b", [True])
    with RunMetrics(path, run_id) as m:
        mine, theirs = m.output_capped_episodes("m-a"), m.output_capped_episodes("m-b")
        data = build_report(m)
    assert (mine["normal"].successes, mine["normal"].n) == (1, 3)  # two capped turns, one episode
    assert (mine["trap"].successes, mine["trap"].n) == (1, 1)  # never blended with normal
    assert mine["normal"].ci_low is not None
    assert (theirs["normal"].successes, theirs["normal"].n) == (1, 1)
    assert data["models"]["m-a"]["output_capped"] == {c: asdict(r) for c, r in mine.items()}
    md = render_markdown(data)
    assert "cut off by the output cap" in md and "1 of 3" in md


def test_the_dry_run_states_what_extra_output_would_cost_without_assuming_the_cap(tmp_path,
                                                                                  capsys):
    from consequence.cli import main

    assert main(["--results-db", str(tmp_path / "r.db"), "run", "--all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "not an upper bound: it does not assume the output cap is reached" in out
    assert "per extra 1000 output tokens per episode" in out
